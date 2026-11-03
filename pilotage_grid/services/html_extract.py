"""
Extraction des jeux de données du fichier HTML « Pilotage Grid » (export autonome) et génération du gabarit d'interface.

Le fichier source embarque ses données dans le script (``const P={...}``, ``const Q={...}``…). Ici on les LIT comme du texte JSON
(rien n'est exécuté) et on remplace chaque littéral par une référence ``__PG.<clé>`` : l'interface reçoit alors ses données depuis
la base (API) et non plus depuis le fichier.
"""
from __future__ import annotations

import json
import re

# (clé base, motif de début du littéral dans le script, nom du bloc d'origine)
_SPECS = [
    ("paiements", re.compile(r"(?m)^const P="), "P"),
    ("sites", re.compile(r"(?:^|;)const Q="), "Q"),
    ("demandes", re.compile(r"(?m)^const DEM=\(\(\)=>\{const D="), "DEM"),
    ("efficacite", re.compile(r"(?m)^const EFF=\(\(\)=>\{const D="), "EFF"),
    ("tarifs", re.compile(r"(?m)^const TAR=\(\(\)=>\{const D="), "TAR"),
    ("cosphi", re.compile(r"(?m)^const COS="), "COS"),
    ("redevance", re.compile(r"(?m)^const D="), "D"),
]


EMBED_CSS = (
    # La navigation par sections est portée par la barre latérale d'EnerTrack : on masque l'en-tête et la barre d'onglets d'origine.
    "header.top{display:none!important}.tabs{display:none!important}"
    ".bar{position:static!important;border-bottom:none!important;padding-top:6px!important}"
)

# Pont avec l'application hôte : reçoit les données (depuis la base) et la section à afficher.
BRIDGE_JS = """
(function(){
  var started=false,pending=null,origin=location.origin;
  function go(id){var t=document.getElementById('t-'+id);if(t)t.click();}
  window.addEventListener('message',function(e){
    if(e.origin!==origin||e.source!==window.parent)return;
    var d=e.data||{};
    if(d.type==='pg:data'&&!started){
      started=true;
      try{window.__pgStart(d.data);if(pending)go(pending);parent.postMessage({type:'pg:started'},origin);}
      catch(err){document.body.insertAdjacentHTML('afterbegin','<p style="padding:16px;color:#B0425A">Erreur d\u2019affichage : '+String(err&&err.message||err)+'</p>');parent.postMessage({type:'pg:error',message:String(err&&err.message||err)},origin);}
    }
    if(d.type==='pg:section'){pending=d.id;if(started)go(d.id);}
  });
  parent.postMessage({type:'pg:ready'},origin);
})();
"""


class ExtractError(ValueError):
    pass


def _inner_document(html: str) -> str:
    i = html.find("<!DOCTYPE html>")
    if i < 0:
        raise ExtractError("Document HTML interne introuvable (<!DOCTYPE html>).")
    j = html.rfind("</html>")
    return html[i:j + len("</html>")] if j > i else html[i:]


def _main_script_span(doc: str) -> tuple[int, int]:
    """(début, fin) du contenu du <script> sans attribut src."""
    m = re.search(r"<script>", doc)
    if not m:
        raise ExtractError("Script principal introuvable.")
    end = doc.find("</script>", m.end())
    if end < 0:
        raise ExtractError("Fin du script principal introuvable.")
    return m.end(), end


def _locate(script: str) -> dict[str, tuple[int, int, object]]:
    dec = json.JSONDecoder()
    found: dict[str, tuple[int, int, object]] = {}
    for key, pat, _name in _SPECS:
        m = pat.search(script)
        if not m:
            raise ExtractError(f"Jeu de données « {key} » introuvable dans le fichier.")
        start = m.end()
        if script[start] not in "{[":
            raise ExtractError(f"Jeu de données « {key} » : littéral JSON attendu.")
        obj, end = dec.raw_decode(script, start)
        found[key] = (start, end, obj)
    return found


def extract_datasets(html: str) -> dict[str, object]:
    doc = _inner_document(html)
    a, b = _main_script_span(doc)
    return {k: v[2] for k, v in _locate(doc[a:b]).items()}


def build_template(html: str, *, chart_src: str = "chart.umd.min.js") -> str:
    """Gabarit d'interface : données remplacées par ``__PG.<clé>``, script différé jusqu'à réception des données."""
    doc = _inner_document(html)
    a, b = _main_script_span(doc)
    script = doc[a:b]
    spans = sorted(((s, e, k) for k, (s, e, _o) in _locate(script).items()), reverse=True)
    for s, e, k in spans:
        script = script[:s] + f"__PG.{k}" + script[e:]
    # Ressources externes retirées : l'interface est autonome (Chart.js servi localement, police système).
    head, tail = doc[:a], doc[b:]
    head = re.sub(r'<link rel="preconnect"[^>]*>\s*', "", head)
    head = re.sub(r'<link href="https://fonts\.googleapis\.com[^>]*>\s*', "", head)
    head = re.sub(r'<script src="https://cdnjs\.cloudflare\.com/[^"]*chart[^"]*"></script>', f'<script src="{chart_src}"></script>', head)
    head = head.replace("</head>", "<style id=\"pg-embed\">/*PG_EMBED_CSS*/</style></head>", 1)
    head = head.replace("<html lang=\"fr\">", "<html lang=\"fr\" data-theme=\"light\">", 1)
    head = head.replace("/*PG_EMBED_CSS*/", EMBED_CSS)
    boot = "window.__pgStart=function(__PG){\n" + script + "\n};\n" + BRIDGE_JS + "\n"
    return head + boot + tail
