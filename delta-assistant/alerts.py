"""delta-alerts: simple price alerts on Delta mark prices. Stored in alerts.json (gitignored).

  python alerts.py add BTCUSD below 79000 --note "support break"
  python alerts.py add XAUTUSD above 4040 --note "move my stop"
  python alerts.py add ETHUSD move 2 --note "2% move from now"
  python alerts.py list
  python alerts.py remove 3
  python alerts.py check [--notify]      # prints triggered alerts once, then marks them done

`check` is meant to be run on a schedule (ask Claude for a /loop or scheduled task), or by hand.
--notify also shows a macOS notification.
"""
import argparse
import json
import subprocess
import sys

from delta_common import HERE, Delta, fmt, now_utc, uk_utc

FILE = HERE / "alerts.json"


def load():
    return json.loads(FILE.read_text()) if FILE.exists() else []


def save(rows):
    FILE.write_text(json.dumps(rows, indent=2))


def notify(text):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Delta alert\""],
                       check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("symbol")
    s.add_argument("kind", choices=["above", "below", "move"])
    s.add_argument("value", type=float, help="price, or %% for move")
    s.add_argument("--note", default="")
    sub.add_parser("list")
    s = sub.add_parser("remove")
    s.add_argument("id", type=int)
    s = sub.add_parser("check")
    s.add_argument("--notify", action="store_true")
    a = ap.parse_args()
    rows, d = load(), Delta()

    if a.cmd == "add":
        sym = a.symbol.upper()
        mark = float(d.ticker(sym)["mark_price"])
        row = {"id": max([r["id"] for r in rows], default=0) + 1, "symbol": sym, "kind": a.kind,
               "value": a.value, "ref": mark, "note": a.note, "created": now_utc().isoformat(), "done": False}
        rows.append(row)
        save(rows)
        print(f"#{row['id']} {sym} {a.kind} {a.value:g}{'%' if a.kind == 'move' else ''}  (now {fmt(mark)})")
    elif a.cmd == "list":
        for r in rows:
            print(f"#{r['id']} {'DONE ' if r['done'] else 'ACTIVE'} {r['symbol']} {r['kind']} {r['value']:g}"
                  f"{'%' if r['kind'] == 'move' else ''}  ref {fmt(r['ref'])}  {r['note']}")
        if not rows:
            print("no alerts")
    elif a.cmd == "remove":
        save([r for r in rows if r["id"] != a.id])
        print(f"removed #{a.id}")
    else:
        hits, marks = [], {}
        for r in rows:
            if r["done"]:
                continue
            m = marks.setdefault(r["symbol"], float(d.ticker(r["symbol"])["mark_price"]))
            hit = (r["kind"] == "above" and m >= r["value"]) or (r["kind"] == "below" and m <= r["value"]) or \
                  (r["kind"] == "move" and abs(m - r["ref"]) / r["ref"] * 100 >= r["value"])
            if hit:
                r["done"], r["fired"] = True, now_utc().isoformat()
                hits.append(f"#{r['id']} {r['symbol']} {r['kind']} {r['value']:g} hit — now {fmt(m)}  {r['note']}")
        save(rows)
        print(f"Alert check {uk_utc()}: {len(hits)} triggered")
        for h in hits:
            print("  " + h)
            if a.notify:
                notify(h)


if __name__ == "__main__":
    main()
