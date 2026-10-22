"""Price quotes for customers, as a PDF.

A quote shows the business's details, the customer, the items with the selling
price (cost plus markup, editable before export), VAT and the total. It never
shows which distributor the item comes from or its cost. The HTML is printed to
PDF by the same Chromium the searches use, which gets Hebrew and right-to-left
layout right.
"""

import html
import time

from .normalize import VAT_RATE


def _money(v):
    return "₪" + f"{v:,.2f}"


def totals(items, vat_rate=VAT_RATE, inclusive=False):
    """(before VAT, VAT, total). With inclusive prices the total is exactly what was typed
    and VAT is worked back out of it."""
    lines = round(sum(i["qty"] * i["unit_price"] for i in items), 2)
    if inclusive:
        subtotal = round(lines / (1 + vat_rate), 2)
        return subtotal, round(lines - subtotal, 2), lines
    vat = round(lines * vat_rate, 2)
    return lines, vat, round(lines + vat, 2)


def _specs_table(it):
    e = html.escape
    rows = "".join(f"<tr><th>{e(s['label'])}</th><td>{e(s['value'])}</td></tr>" for s in it.get("specs") or [])
    if not rows:
        return ""
    # the address keeps its own direction inside the Hebrew line
    source = (f'<div class="src">מקור: ' + " · ".join(f"<bdi>{e(part.strip())}</bdi>" for part in
              it["spec_source"].split("·")) + "</div>") if it.get("spec_source") else ""
    return f'<table class="specs">{rows}</table>{source}'


def quote_html(biz, number, items, customer="", customer_contact="", notes="",
               show_images=True, vat_rate=VAT_RATE, today=None, inclusive=False):
    e = html.escape
    today = today or time.time()
    date = time.strftime("%d/%m/%Y", time.localtime(today))
    valid = time.strftime("%d/%m/%Y", time.localtime(today + int(biz.get("valid_days") or 14) * 86400))
    subtotal, vat, total = totals(items, vat_rate, inclusive)
    price_note = ' (כולל מע"מ)' if inclusive else ""

    # <bdi> keeps numbers / addresses in their own direction inside the Hebrew line
    contact = " · ".join(f"<bdi>{e(x)}</bdi>" for x in (biz.get("phone"), biz.get("email"), biz.get("address")) if x)
    logo = f'<img class="logo" src="{e(biz["logo"])}" alt="">' if biz.get("logo") else ""
    rows = []
    for n, it in enumerate(items, 1):
        img = (f'<img src="{e(it["image"])}" alt="">' if show_images and it.get("image") else "")
        mpn = f'<div class="mpn">מק"ט: <bdi>{e(it["mpn"])}</bdi></div>' if it.get("mpn") else ""
        rows.append(f"""<tr><td class="n">{n}</td><td class="img">{img}</td>
          <td><div class="title">{e(it["title"])}</div>{mpn}</td>
          <td class="num">{it["qty"]:g}</td><td class="num">{_money(it["unit_price"])}</td>
          <td class="num">{_money(it["qty"] * it["unit_price"])}</td></tr>""")
        specs = _specs_table(it)
        if specs:
            rows.append(f'<tr class="spec-row"><td></td><td></td><td colspan="4">{specs}</td></tr>')
    notes_html = f'<div class="notes"><b>הערות:</b> {e(notes)}</div>' if notes else ""
    customer_html = (f'<div class="to"><div class="label">לכבוד</div><div class="cust">{e(customer)}</div>'
                     f'<div><bdi>{e(customer_contact)}</bdi></div></div>') if customer or customer_contact else ""
    footer = f'<div class="footer">{e(biz["footer"])}</div>' if biz.get("footer") else ""
    biz_id = f'<div>ח.פ / ע.מ: <bdi>{e(biz["business_id"])}</bdi></div>' if biz.get("business_id") else ""

    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Segoe UI", Arial, "Liberation Sans", "FreeSans", sans-serif; color: #1d2433; font-size: 12.5px; margin: 0; }}
  .head {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 3px solid #1f5eff; padding-bottom: 12px; }}
  .biz h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .biz div {{ color: #555; }}
  .logo {{ max-height: 70px; max-width: 200px; }}
  .meta {{ display: flex; justify-content: space-between; margin: 18px 0; gap: 20px; }}
  .doc h2 {{ font-size: 20px; margin: 0 0 6px; color: #1f5eff; }}
  .label {{ color: #777; font-size: 11px; }}
  .cust {{ font-size: 15px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f0f3fa; text-align: right; padding: 8px 6px; font-size: 11.5px; color: #555; }}
  td {{ border-bottom: 1px solid #e3e7ee; padding: 8px 6px; vertical-align: middle; }}
  td.n {{ width: 24px; color: #777; }}
  td.img {{ width: 64px; }}
  td.img img {{ width: 56px; height: 56px; object-fit: contain; }}
  .title {{ font-weight: 600; }}
  .mpn {{ color: #777; font-size: 11px; margin-top: 2px; }}
  .num {{ white-space: nowrap; text-align: left; direction: ltr; }}
  tr.spec-row td {{ padding-top: 0; }}
  table.specs {{ width: 100%; border-collapse: collapse; margin: 2px 0 4px; font-size: 11px; }}
  table.specs th {{ background: none; color: #555; font-weight: 600; width: 28%; padding: 3px 6px;
                    border-bottom: 1px solid #f0f2f5; vertical-align: top; }}
  table.specs td {{ border-bottom: 1px solid #f0f2f5; padding: 3px 6px; unicode-bidi: plaintext; }}
  table.specs tr {{ break-inside: avoid; }}
  .src {{ color: #999; font-size: 10px; text-align: right; }}
  .sum {{ width: 280px; margin: 14px 0 0 auto; margin-right: auto; margin-left: 0; }}
  .sum td {{ border: 0; padding: 4px 6px; }}
  .sum tr.total td {{ border-top: 2px solid #1d2433; font-size: 15px; font-weight: 700; padding-top: 8px; }}
  .notes {{ margin-top: 18px; padding: 10px 12px; background: #fafbfc; border-radius: 6px; }}
  .terms {{ margin-top: 18px; color: #555; font-size: 11.5px; }}
  .footer {{ margin-top: 24px; color: #777; font-size: 11px; text-align: center; }}
</style></head><body>
<div class="head">
  <div class="biz"><h1>{e(biz.get("business_name") or "הצעת מחיר")}</h1>{biz_id}<div>{contact}</div></div>
  {logo}
</div>
<div class="meta">
  {customer_html}
  <div class="doc"><h2>הצעת מחיר מס' {number}</h2>
    <div>תאריך: {date}</div><div>בתוקף עד: {valid}</div></div>
</div>
<table>
  <thead><tr><th>#</th><th></th><th>פריט</th><th class="num">כמות</th><th class="num">מחיר ליחידה{price_note}</th><th class="num">סה"כ{price_note}</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
<table class="sum">
  <tr><td>סה"כ לפני מע"מ</td><td class="num">{_money(subtotal)}</td></tr>
  <tr><td>מע"מ {vat_rate * 100:g}%</td><td class="num">{_money(vat)}</td></tr>
  <tr class="total"><td>סה"כ לתשלום</td><td class="num">{_money(total)}</td></tr>
</table>
{notes_html}
<div class="terms">המחירים בשקלים. ההצעה בתוקף עד {valid} ובכפוף לזמינות המלאי.</div>
{footer}
</body></html>"""
