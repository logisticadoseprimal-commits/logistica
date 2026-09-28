#!/usr/bin/env python3
"""
Separa o PDF de etiquetas do dia em PDFs por quantidade de potes,
cruzando cada etiqueta com o export de pedidos da Yampi, e gera um
relatório de possíveis erros.

Uso:
    python3 separar_etiquetas.py etiquetas.pdf pedidos_yampi.csv [pasta_saida]

Regra de potes: soma de `quantidade` de todas as linhas do pedido
(kit + order bump). Ex.: kit 3 (R$297) + bump 1 (R$65) = 4 potes.

Dependência: pip install pymupdf
"""
import csv
import re
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf

KITS_CONHECIDOS = {1, 2, 3, 4, 6, 7, 8, 9}
# status da Yampi que indicam que o pedido NÃO deveria ganhar etiqueta nova
STATUS_SUSPEITOS = {"em transporte", "entregue", "cancelado", "estornado",
                    "aguardando pagamento", "pagamento recusado"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def similaridade_nome(a, b):
    """0..1 — tolera nome truncado na etiqueta (J&T corta ~32 caracteres)."""
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    ja, jb = " ".join(ta), " ".join(tb)
    if ja.startswith(jb) or jb.startswith(ja):
        return 1.0
    ratio = SequenceMatcher(None, ja, jb).ratio()
    comuns = len(set(ta) & set(tb)) / min(len(set(ta)), len(set(tb)))
    return max(ratio, comuns)


def ler_etiquetas(pdf_path):
    doc = pymupdf.open(pdf_path)
    etiquetas = []
    for i, page in enumerate(doc):
        linhas = [l.strip() for l in page.get_text().splitlines()]
        et = {"pagina": i, "nome": "", "cep": "", "cidade": "", "nfe": "",
              "rastreio": "", "chave_nfe": "", "endereco": "", "remetente": ""}
        if "DESTINATÁRIO" in linhas:
            j = linhas.index("DESTINATÁRIO")
            et["nome"] = linhas[j + 1]
            end = []
            for l in linhas[j + 2:]:
                m = re.match(r"^(\d{8})\s+(.*)$", l)
                if m:
                    et["cep"], et["cidade"] = m.group(1), m.group(2)
                    break
                end.append(l)
            et["endereco"] = " ".join(end)
        if "REMETENTE:" in linhas:
            k = linhas.index("REMETENTE:")
            et["remetente"] = " ".join(linhas[k + 1:k + 4])
        for l in linhas:
            if m := re.search(r"NFe N°\.\s*(\d+)", l):
                et["nfe"] = str(int(m.group(1)))
            if re.fullmatch(r"\d{44}", l):
                et["chave_nfe"] = l
            if re.fullmatch(r"8880\d{11}", l) and not et["rastreio"]:
                et["rastreio"] = l
        etiquetas.append(et)
    return doc, etiquetas


def ler_pedidos(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        linhas = list(csv.DictReader(f))
    pedidos = defaultdict(lambda: {"itens": []})
    for r in linhas:
        p = pedidos[r["numero_pedido"]]
        p["numero"] = r["numero_pedido"]
        p["cliente"] = r["cliente"]
        p["destinatario"] = r["entrega_destinatario"] or r["cliente"]
        p["cep"] = re.sub(r"\D", "", r["entrega_cep"]).zfill(8)
        p["status"] = r["status"]
        p["data"] = r["data"]
        p["endereco"] = r["entrega_endereco"]
        p["numero_end"] = r["entrega_numero"]
        p["cpf"] = r["cliente_document"]
        p["itens"].append((int(r["quantidade"] or 0), r["total_item"], r["produto"]))
    for p in pedidos.values():
        p["potes"] = sum(q for q, _, _ in p["itens"])
    return list(pedidos.values())


def casar(etiquetas, pedidos):
    por_cep = defaultdict(list)
    for p in pedidos:
        por_cep[p["cep"]].append(p)
    for et in etiquetas:
        candidatos = []
        for p in por_cep.get(et["cep"], []):
            s = max(similaridade_nome(et["nome"], p["destinatario"]),
                    similaridade_nome(et["nome"], p["cliente"]))
            candidatos.append((s, p))
        if not candidatos:  # fallback: só pelo nome, em qualquer CEP
            for p in pedidos:
                s = max(similaridade_nome(et["nome"], p["destinatario"]),
                        similaridade_nome(et["nome"], p["cliente"]))
                if s >= 0.85:
                    candidatos.append((s - 0.01, p))  # marca como "sem CEP"
        candidatos.sort(key=lambda c: -c[0])
        et["candidatos"] = candidatos
        et["pedido"] = candidatos[0][1] if candidatos and candidatos[0][0] >= 0.6 else None
        et["score"] = candidatos[0][0] if candidatos else 0.0


def verificar(etiquetas, pedidos):
    erros = []  # (gravidade, pagina|None, mensagem)

    def add(g, et, msg):
        erros.append((g, et["pagina"] + 1 if et else None, msg))

    usados = defaultdict(list)
    por_nfe = defaultdict(list)
    for et in etiquetas:
        por_nfe[et["nfe"] or et["chave_nfe"]].append(et)
        p = et["pedido"]
        if not p:
            add("ALTO", et, f"Etiqueta de '{et['nome']}' (CEP {et['cep']}) sem pedido na Yampi.")
            continue
        usados[p["numero"]].append(et)
        if p["cep"] != et["cep"]:
            add("ALTO", et, f"'{et['nome']}': CEP da etiqueta {et['cep']} ≠ CEP Yampi {p['cep']}.")
        if et["score"] < 0.9:
            add("MÉDIO", et, f"Nome diverge: etiqueta '{et['nome']}' x Yampi '{p['destinatario']}'.")
        if p["status"].lower() in STATUS_SUSPEITOS:
            add("ALTO", et, f"'{et['nome']}': pedido {p['numero']} está '{p['status']}' na Yampi.")
        if p["potes"] not in KITS_CONHECIDOS:
            add("MÉDIO", et, f"'{et['nome']}': {p['potes']} potes não é um kit conhecido.")
        num = norm(p["numero_end"])
        if num and num[0] not in norm(et["endereco"]):
            add("BAIXO", et, f"'{et['nome']}': nº do endereço Yampi '{p['numero_end']}' não aparece na etiqueta.")
        mesmos = [q for s, q in et["candidatos"] if s >= 0.6]
        if len(mesmos) > 1:
            info = ", ".join(f"{q['numero']} ({q['potes']} potes, {q['status']})" for q in mesmos)
            add("ALTO", et, f"'{et['nome']}' tem {len(mesmos)} pedidos na Yampi: {info}. "
                            f"Separado com o mais provável — CONFERIR se vão juntos.")

    for nfe, ets in por_nfe.items():
        if len(ets) > 1:
            pags = ", ".join(str(e["pagina"] + 1) for e in ets)
            rast = ", ".join(e["rastreio"] for e in ets)
            add("ALTO", ets[0], f"NF-e {nfe} tem {len(ets)} etiquetas (págs {pags}; rastreios {rast}). "
                                f"Etiqueta DUPLICADA — usar só uma e cancelar a outra na J&T.")
    for num, ets in usados.items():
        nfes = {e["nfe"] for e in ets}
        if len(ets) > 1 and len(nfes) > 1:
            add("ALTO", ets[0], f"Pedido {num} casou com {len(ets)} etiquetas de NF-es diferentes ({', '.join(sorted(nfes))}).")

    for p in pedidos:
        if p["numero"] not in usados and p["status"].lower() in ("faturado", "pagamento aprovado"):
            erros.append(("INFO", None, f"Pedido {p['numero']} de {p['data'][:10]} ({p['destinatario']}, "
                                        f"{p['potes']} potes, '{p['status']}') sem etiqueta neste PDF."))

    remetentes = defaultdict(list)
    for et in etiquetas:
        remetentes[et["remetente"]].append(et["pagina"] + 1)
    if len(remetentes) > 1:
        for rem, pags in remetentes.items():
            if len(pags) < len(etiquetas) / 2:
                add("MÉDIO", None, f"Remetente diferente do padrão nas págs {pags}: '{rem}'.")

    ordem = {"ALTO": 0, "MÉDIO": 1, "BAIXO": 2, "INFO": 3}
    erros.sort(key=lambda e: (ordem[e[0]], e[1] or 0))
    return erros, usados


def main():
    pdf_path, csv_path = sys.argv[1], sys.argv[2]
    saida = Path(sys.argv[3] if len(sys.argv) > 3 else "saida")
    saida.mkdir(parents=True, exist_ok=True)

    doc, etiquetas = ler_etiquetas(pdf_path)
    pedidos = ler_pedidos(csv_path)
    casar(etiquetas, pedidos)
    erros, usados = verificar(etiquetas, pedidos)

    grupos = defaultdict(list)
    for et in etiquetas:
        chave = f"{et['pedido']['potes']:02d}_potes" if et["pedido"] else "SEM_PEDIDO"
        grupos[chave].append(et)

    for chave, ets in sorted(grupos.items()):
        out = pymupdf.open()
        for et in ets:
            out.insert_pdf(doc, from_page=et["pagina"], to_page=et["pagina"])
        out.save(saida / f"etiquetas_{chave}.pdf")

    with open(saida / "conferencia.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["pagina", "nome_etiqueta", "cep", "nfe", "rastreio", "pedido_yampi",
                    "nome_yampi", "status_yampi", "potes", "itens", "arquivo"])
        for et in etiquetas:
            p = et["pedido"] or {}
            itens = " + ".join(f"{q}x R${v}" for q, v, _ in p.get("itens", []))
            chave = f"{p['potes']:02d}_potes" if p else "SEM_PEDIDO"
            w.writerow([et["pagina"] + 1, et["nome"], et["cep"], et["nfe"], et["rastreio"],
                        p.get("numero", ""), p.get("destinatario", ""), p.get("status", ""),
                        p.get("potes", ""), itens, f"etiquetas_{chave}.pdf"])

    linhas = ["RESUMO POR QUANTIDADE DE POTES", "=" * 40]
    total_potes = 0
    for chave, ets in sorted(grupos.items()):
        n = len(ets)
        if chave != "SEM_PEDIDO":
            total_potes += int(chave[:2]) * n
        linhas.append(f"{chave:>12}: {n:3d} etiqueta(s)")
    linhas += [f"{'TOTAL':>12}: {len(etiquetas):3d} etiquetas | {total_potes} potes", "",
               "POSSÍVEIS ERROS", "=" * 40]
    linhas += [f"[{g}] {'pág ' + str(pg) if pg else 'geral'}: {m}" for g, pg, m in erros] or ["Nenhum."]
    (saida / "relatorio.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print("\n".join(linhas))


if __name__ == "__main__":
    main()
