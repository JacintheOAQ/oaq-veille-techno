#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecte des nouveautes en sante auditive.

Trois sources :
  - Europe PMC  : litterature biomedicale mondiale, PubMed inclus, avec resumes
  - Crossref    : toutes les revues avec un DOI, au-dela du biomedical
  - Flux RSS    : organismes et presse specialisee

Resultat : ajout des nouveautes dans donnees/candidats.json.
Rien n'est publie sur le site tant qu'une personne n'a pas trie.

Aucune dependance a installer. Bibliotheque standard seulement.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F_SOURCES = os.path.join(RACINE, "scripts", "sources.json")
F_CANDIDATS = os.path.join(RACINE, "donnees", "candidats.json")
F_VUS = os.path.join(RACINE, "donnees", "vus.json")

AGENT = "OAQ-veille/1.0 (veille en sante auditive; contact via GitHub)"

# Garde-fou thematique : un document dont le titre ou le resume ne contient
# aucun de ces mots n'a rien a voir avec l'audition. Ajoutez-en au besoin.
MOTS_AUDITION = [
    "hearing", "hear ", "audiolog", "audiomet", "audiogram", "auditory",
    "tinnitus", "cochlea", "deaf", "otolog", "otitis", "presbycus",
    "earmold", "ear mold", "hearable", "amplification", "sound exposure",
    "noise-induced", "audition", "auditive", "auditif", "surdite", "surdité",
    "acouphene", "acouphène", "prothese auditive", "prothèse auditive",
]


def pertinent(fiche):
    texte = (str(fiche.get("titre", "")) + " " + str(fiche.get("resume_source", ""))).lower()
    return any(mot in texte for mot in MOTS_AUDITION)
PAUSE = 1.0  # seconde entre deux appels, par courtoisie envers les serveurs

incidents = []


# --------------------------------------------------------------- utilitaires

def lire_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as f:
            contenu = f.read().strip()
            return json.loads(contenu) if contenu else defaut
    except FileNotFoundError:
        return defaut
    except json.JSONDecodeError as e:
        incidents.append("Fichier illisible : %s (%s)" % (chemin, e))
        return defaut


def ecrire_json(chemin, donnees):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=2)
        f.write("\n")


def appeler(url):
    requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(requete, timeout=45) as reponse:
        return reponse.read()


def appeler_json(url, etiquette):
    try:
        brut = appeler(url)
        return json.loads(brut.decode("utf-8", "replace"))
    except Exception as e:
        incidents.append("%s : %s" % (etiquette, e))
        return None


def slug(texte, longueur=60):
    t = unicodedata.normalize("NFD", str(texte or ""))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:longueur] or "sans-titre"


def nettoyer(texte, limite=1600):
    t = re.sub(r"<[^>]+>", " ", str(texte or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limite] + ("..." if len(t) > limite else "")


def cle(doi, titre):
    return "doi:" + doi.lower().strip() if doi else "titre:" + slug(titre, 90)


def date_iso(*morceaux):
    """Construit une date AAAA-MM-JJ a partir de ce qui est disponible."""
    for m in morceaux:
        if not m:
            continue
        s = str(m)
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:10]
        if re.match(r"^\d{4}-\d{2}$", s):
            return s + "-01"
        if re.match(r"^\d{4}$", s):
            return s + "-01-01"
    return ""


# --------------------------------------------------------------- Europe PMC

def europepmc(requete, depuis, maximum):
    terme = "(%s) AND (FIRST_PDATE:[%s TO 3000-01-01])" % (requete, depuis)
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?query=%s&format=json&resultType=core&pageSize=%d&sort=P_PDATE_D%%20desc"
           % (urllib.parse.quote(terme), maximum))
    donnees = appeler_json(url, "Europe PMC")
    if not donnees:
        return []

    sortie = []
    for r in donnees.get("resultList", {}).get("result", []):
        doi = r.get("doi", "")
        titre = nettoyer(r.get("title", ""), 400)
        if not titre:
            continue
        lien = ("https://doi.org/" + doi) if doi else (
            "https://europepmc.org/article/%s/%s" % (r.get("source", "MED"), r.get("id", "")))
        auteurs = nettoyer(r.get("authorString", ""), 300)
        sortie.append({
            "cle": cle(doi, titre),
            "titre": titre,
            "auteurs": auteurs,
            "source": r.get("journalTitle") or r.get("bookOrReportDetails", {}).get("publisher") or "Europe PMC",
            "date": date_iso(r.get("firstPublicationDate"), r.get("pubYear")),
            "url": lien,
            "resume_source": nettoyer(r.get("abstractText", "")),
            "origine": "Europe PMC",
        })
    return sortie


# ------------------------------------------------------------------ Crossref

def crossref(requete, depuis, jusqua, maximum):
    url = ("https://api.crossref.org/works"
           "?query=%s&filter=from-pub-date:%s,until-pub-date:%s,type:journal-article"
           "&rows=%d&select=DOI,title,abstract,author,container-title,published"
           % (urllib.parse.quote(requete), depuis, jusqua, maximum))
    donnees = appeler_json(url, "Crossref")
    if not donnees:
        return []

    sortie = []
    for r in donnees.get("message", {}).get("items", []):
        titres = r.get("title") or []
        titre = nettoyer(titres[0] if titres else "", 400)
        if not titre:
            continue
        doi = r.get("DOI", "")
        parties = (r.get("published", {}).get("date-parts") or [[]])[0]
        date = "-".join("%02d" % p if i else str(p) for i, p in enumerate(parties[:3]))
        auteurs = ", ".join(
            " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x)
            for a in (r.get("author") or [])[:8]
        )
        revues = r.get("container-title") or []
        sortie.append({
            "cle": cle(doi, titre),
            "titre": titre,
            "auteurs": nettoyer(auteurs, 300),
            "source": nettoyer(revues[0] if revues else "Crossref", 200),
            "date": date_iso(date),
            "url": "https://doi.org/" + doi if doi else "",
            "resume_source": nettoyer(r.get("abstract", "")),
            "origine": "Crossref",
        })
    return sortie


# ----------------------------------------------------------------------- RSS

def rss(nom, url, filtre, depuis_dt):
    try:
        brut = appeler(url)
        arbre = ET.fromstring(brut)
    except Exception as e:
        incidents.append("Flux %s indisponible : %s" % (nom, e))
        return []

    entrees = arbre.iter("item")
    articles = list(entrees)
    if not articles:  # format Atom
        articles = [e for e in arbre.iter() if e.tag.endswith("}entry")]

    def champ(el, *noms):
        for n in noms:
            trouve = el.find(n)
            if trouve is None:
                for enfant in el:
                    if enfant.tag.endswith("}" + n):
                        trouve = enfant
                        break
            if trouve is not None:
                if n == "link" and not (trouve.text or "").strip():
                    return trouve.get("href", "")
                return (trouve.text or "").strip()
        return ""

    sortie = []
    for a in articles:
        titre = nettoyer(champ(a, "title"), 400)
        if not titre:
            continue
        texte = (titre + " " + champ(a, "description", "summary", "content")).lower()
        if filtre and not any(mot.lower() in texte for mot in filtre):
            continue
        brut_date = champ(a, "pubDate", "published", "updated", "date")
        date = ""
        for forme in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                      "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                d = datetime.strptime(brut_date.replace("GMT", "+0000"), forme)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if d < depuis_dt:
                    date = None
                    break
                date = d.strftime("%Y-%m-%d")
                break
            except (ValueError, AttributeError):
                continue
        if date is None:
            continue
        sortie.append({
            "cle": cle("", titre),
            "titre": titre,
            "auteurs": "",
            "source": nom,
            "date": date,
            "url": champ(a, "link"),
            "resume_source": nettoyer(champ(a, "description", "summary", "content")),
            "origine": nom,
        })
    return sortie


# ---------------------------------------------------------------------- main

def main():
    config = lire_json(F_SOURCES, {})
    if not config:
        print("Fichier sources.json introuvable ou vide.")
        return 1

    jours = int(config.get("fenetre_jours", 14))
    maximum = int(config.get("max_par_requete", 25))
    depuis_dt = datetime.now(timezone.utc) - timedelta(days=jours)
    depuis = depuis_dt.strftime("%Y-%m-%d")
    aujourdhui = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    candidats = lire_json(F_CANDIDATS, [])
    vus = lire_json(F_VUS, [])

    # Tolerance : un fichier mal forme est ignore au lieu de bloquer la collecte
    if not isinstance(candidats, list):
        candidats = []
    if not isinstance(vus, list):
        vus = []
    candidats = [c for c in candidats if isinstance(c, dict)]
    vus = [v for v in vus if isinstance(v, str)]

    connus = set(vus)
    for c in candidats:
        connus.add(c.get("cle") or cle("", c.get("titre", "")))
        if c.get("id"):
            connus.add(c["id"])

    recoltes = []
    for r in config.get("requetes", []):
        if r.get("europepmc"):
            recoltes += [dict(x, theme=r["nom"]) for x in europepmc(r["europepmc"], depuis, maximum)]
            time.sleep(PAUSE)
        if r.get("crossref"):
            recoltes += [dict(x, theme=r["nom"]) for x in crossref(r["crossref"], depuis, aujourdhui, maximum)]
            time.sleep(PAUSE)

    for f in config.get("flux_rss", []):
        recoltes += [dict(x, theme=f.get("nom", "")) for x in rss(f["nom"], f["url"], f.get("filtre", []), depuis_dt)]
        time.sleep(PAUSE)

    # Garde-fou : une date future ou absurde signale une metadonnee fautive
    plancher = (datetime.now(timezone.utc) - timedelta(days=jours + 400)).strftime("%Y-%m-%d")
    propres, aberrants, hors_sujet = [], 0, 0
    for r in recoltes:
        d = r.get("date") or ""
        if d and (d > aujourdhui or d < plancher):
            aberrants += 1
            continue
        if r.get("origine") in ("Europe PMC", "Crossref") and not pertinent(r):
            hors_sujet += 1
            continue
        propres.append(r)
    recoltes = propres

    nouveaux = []
    for r in recoltes:
        k = r["cle"]
        if k in connus:
            continue
        connus.add(k)
        identifiant = slug(r["titre"], 48)
        base, n = identifiant, 2
        while identifiant in connus:
            identifiant = "%s-%d" % (base, n)
            n += 1
        connus.add(identifiant)
        r["id"] = identifiant
        r["statut"] = "attente"
        nouveaux.append(r)

    nouveaux.sort(key=lambda x: x.get("date", ""), reverse=True)
    ecrire_json(F_CANDIDATS, candidats + nouveaux)

    # Rapport lisible dans l'onglet Actions de GitHub
    lignes = [
        "## Collecte du %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "",
        "- Fenetre : %d jours, depuis le %s" % (jours, depuis),
        "- Resultats examines : %d" % len(recoltes),
        "- Ecartes pour date aberrante : %d" % aberrants,
        "- Ecartes hors sujet : %d" % hors_sujet,
        "- **Nouveaux candidats : %d**" % len(nouveaux),
        "- Total en attente de triage : %d" % (len(candidats) + len(nouveaux)),
        "",
    ]
    if incidents:
        lignes += ["### Incidents", ""] + ["- " + i for i in incidents] + [""]
    if nouveaux:
        lignes += ["### Dix plus recents", ""]
        for x in nouveaux[:10]:
            lignes.append("- %s, *%s*, %s" % (x["titre"][:110], x["source"][:60], x.get("date") or "date inconnue"))

    rapport = "\n".join(lignes)
    print(rapport)

    resume = os.environ.get("GITHUB_STEP_SUMMARY")
    if resume:
        with open(resume, "a", encoding="utf-8") as f:
            f.write(rapport + "\n")

    sortie = os.environ.get("GITHUB_OUTPUT")
    if sortie:
        with open(sortie, "a", encoding="utf-8") as f:
            f.write("nouveaux=%d\n" % len(nouveaux))
            f.write("incidents=%d\n" % len(incidents))

    return 0


if __name__ == "__main__":
    sys.exit(main())
