"""
Robô 163 News -> Instagram
Verifica notícias novas no site, gera a arte pelo "Gerar post"/"Gerar storie"
do painel e publica no Instagram pela API oficial da Meta.
"""
import io
import json
import os
import re
import sys
import time
import subprocess
from html import unescape

import requests
from PIL import Image

SITE = "https://163news.com.br"
GRAPH = "https://graph.facebook.com/v27.0"

PAINEL_EMAIL = os.environ["PAINEL_EMAIL"]
PAINEL_SENHA = os.environ["PAINEL_SENHA"]
IG_USER_ID = os.environ["IG_USER_ID"]
META_TOKEN = os.environ["META_TOKEN"]

POSTAR_FEED = os.environ.get("POSTAR_FEED", "sim").lower() == "sim"
POSTAR_STORIES = os.environ.get("POSTAR_STORIES", "nao").lower() == "sim"
MAX_POR_RODADA = int(os.environ.get("MAX_POR_RODADA", "3"))
HASHTAGS = os.environ.get("HASHTAGS", "#163News #MatoGrosso #Notícias")

# URL pública onde as artes ficam hospedadas (pasta artes/ deste repositório)
REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
URL_ARTES = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/artes"

ESTADO = "estado.json"
UA = {"User-Agent": "Mozilla/5.0 (Robo163News)"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def carregar_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"ultimo_id": None, "postados": []}


def salvar_estado(e):
    e["postados"] = e["postados"][-300:]
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=1)


def noticias_publicadas():
    """IDs e links das notícias que aparecem na página inicial (só publicadas)."""
    html = requests.get(SITE + "/", headers=UA, timeout=30).text
    achadas = {}
    for m in re.finditer(r'href="(?:https://163news\.com\.br)?(/noticia/(\d+)/[^"?#]*)', html):
        achadas[int(m.group(2))] = SITE + m.group(1)
    return dict(sorted(achadas.items()))


def dados_noticia(url):
    html = requests.get(url, headers=UA, timeout=30).text

    def meta(prop):
        m = re.search(r'<meta[^>]+property="' + prop + r'"[^>]+content="([^"]*)"', html)
        return unescape(m.group(1)).strip() if m else ""

    titulo = meta("og:title").replace(" - 163 News", "").strip()
    return {"titulo": titulo, "resumo": meta("og:description")}


def login_painel():
    s = requests.Session()
    s.headers.update(UA)
    s.get(SITE + "/admin/", timeout=30)
    r = s.post(SITE + "/admin/login.php",
               data={"continua": "/admin/", "email": PAINEL_EMAIL, "password": PAINEL_SENHA},
               timeout=30)
    if "login.php" in r.url or 'name="password"' in r.text:
        sys.exit("ERRO: não consegui entrar no painel. Confira PAINEL_EMAIL e PAINEL_SENHA.")
    log("Login no painel OK")
    return s


def baixar_arte(sessao, nid, tipo):
    r = sessao.get(f"{SITE}/admin/plugins/gera_post/gera.php",
                   params={"id": nid, "tipo": tipo}, timeout=60)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")  # Instagram só aceita JPG
    os.makedirs("artes", exist_ok=True)
    nome = f"{nid}-{tipo}.jpg"
    img.save(os.path.join("artes", nome), "JPEG", quality=92)
    return nome


def publicar_github(arquivos):
    """Sobe as artes pro repositório pra terem um link público que o Instagram consiga baixar."""
    subprocess.run(["git", "add", *arquivos], check=True)
    subprocess.run(["git", "commit", "-m", "artes novas"], check=True)
    subprocess.run(["git", "push"], check=True)
    time.sleep(20)  # dá tempo do link público ficar disponível


def ig_publicar(image_url, legenda=None, story=False):
    dados = {"image_url": image_url, "access_token": META_TOKEN}
    if story:
        dados["media_type"] = "STORIES"
    else:
        dados["caption"] = legenda
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media", data=dados, timeout=60).json()
    if "id" not in r:
        raise RuntimeError(f"Erro ao criar mídia: {r}")
    cid = r["id"]
    for _ in range(12):  # espera o Instagram processar a imagem
        st = requests.get(f"{GRAPH}/{cid}", params={"fields": "status_code", "access_token": META_TOKEN}, timeout=30).json()
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram recusou a imagem: {st}")
        time.sleep(5)
    r = requests.post(f"{GRAPH}/{IG_USER_ID}/media_publish",
                      data={"creation_id": cid, "access_token": META_TOKEN}, timeout=60).json()
    if "id" not in r:
        raise RuntimeError(f"Erro ao publicar: {r}")
    return r["id"]


def montar_legenda(d):
    partes = [d["titulo"]]
    if d["resumo"] and d["resumo"] != d["titulo"]:
        partes.append(d["resumo"])
    partes.append("📲 Leia a matéria completa no link da bio ou em 163news.com.br")
    partes.append(HASHTAGS)
    return "\n\n".join(partes)[:2200]


def main():
    estado = carregar_estado()
    noticias = noticias_publicadas()
    if not noticias:
        sys.exit("ERRO: não encontrei notícias na página inicial.")

    if estado["ultimo_id"] is None:
        # Primeira execução: só marca o ponto de partida, não posta notícias antigas.
        estado["ultimo_id"] = max(noticias)
        salvar_estado(estado)
        log(f"Primeira execução. Começando a partir da notícia {estado['ultimo_id']}.")
        return

    novas = [(i, u) for i, u in noticias.items() if i > estado["ultimo_id"] and i not in estado["postados"]]
    if not novas:
        log("Nenhuma notícia nova.")
        return
    novas = novas[:MAX_POR_RODADA]
    log(f"{len(novas)} notícia(s) nova(s): {[i for i, _ in novas]}")

    sessao = login_painel()
    artes = {}
    for nid, _ in novas:
        if POSTAR_FEED:
            artes[(nid, "post")] = baixar_arte(sessao, nid, "post")
        if POSTAR_STORIES:
            artes[(nid, "storie")] = baixar_arte(sessao, nid, "storie")
    publicar_github([os.path.join("artes", n) for n in artes.values()])

    for nid, url in novas:
        try:
            d = dados_noticia(url)
            if POSTAR_FEED:
                pid = ig_publicar(f"{URL_ARTES}/{artes[(nid, 'post')]}", montar_legenda(d))
                log(f"Feed OK: notícia {nid} -> post {pid} | {d['titulo']}")
            if POSTAR_STORIES:
                sid = ig_publicar(f"{URL_ARTES}/{artes[(nid, 'storie')]}", story=True)
                log(f"Story OK: notícia {nid} -> {sid}")
            estado["postados"].append(nid)
        except Exception as e:
            log(f"FALHOU notícia {nid}: {e}")
            estado["postados"].append(nid)  # não fica tentando a mesma pra sempre
        estado["ultimo_id"] = max(estado["ultimo_id"], nid)
        salvar_estado(estado)
        time.sleep(10)


if __name__ == "__main__":
    main()
