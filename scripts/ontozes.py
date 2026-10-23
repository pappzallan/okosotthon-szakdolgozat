#!/usr/bin/env python3
"""
ontozes.py - idojaras-alapu gyepontozes talajvizmerleggel (FAO-56)

Ket uzemmod:

  python3 ontozes.py backfill --start 2026-04-01 --end 2026-10-15 [--from-file F]
      Letolti a megadott idoszak napi idojarasi adatait (Open-Meteo, ERA5
      reanalizis), lefuttatja a szezon szimulaciojat, es negy fajlt ir:
        ontozes_idojaras.csv    - a letoltott napi adatok
        ontozes_szimulacio.csv  - napi vizmerleg es ontozes harom strategiaval
        ontozes_havi.csv        - havi osszesites
        ontozes_osszegzes.json  - szezonos osszesites

  python3 ontozes.py today
      A legutobbi futas ota eltelt napok adataival frissiti a talaj
      vizhianyat, a mai elorejelzes alapjan eldonti, kell-e ma ontozni,
      es egy sor JSON-t ir ki (a Home Assistant ezt olvassa be).
      Egy napon belul tobbszor futtatva ugyanazt a dontest adja vissza.

A modszer (FAO-56, 8. fejezet, egyszerusitett talajvizmerleg):
  gyep napi vizigenye          ETc = Ks * Kc * ET0                    [mm = l/m2]
  hasznos csapadek             P_eff = P, ha P >= 0,2 * ET0, kulonben 0
  gyokerzona vizhianya         Dr(t) = max(0, Dr(t-1) + ETc - P_eff - I)
  felveheto vizkeszlet         TAW = (thetaFC - thetaWP) * Zr
  konnyen felveheto keszlet    RAW = p * TAW      -> ontozesi kuszob
  stresszegyutthato            Ks = 1, ha Dr <= RAW, kulonben (TAW - Dr) / (TAW - RAW)
  stresszes nap                Ks < 1 (a gyep kevesebbet parologtat, mint amennyit tudna)
  ontozesi ido zonankent       perc = I / csapadekintenzitas * 60

Harom strategia ugyanazon az idojarason:
  okos    - hajnalban ontoz, ha Dr >= RAW, es pontosan a hianyt potolja; ha
            aznapra legalabb rain_skip_mm eso varhato, elhalasztja (a
            visszamenoleges futasban az elorejelzest az aznapi tenyleges
            csapadek adja)
  fix     - a mostani idozites zonankent (1. zona 15 perc, 4. zona 30 perc
            naponta), esotol fuggetlenul; a ket zonat kulon szamolja
  hangolt - fix idozites havonta beallitva a honap atlagos netto vizigenyere
            (a legjobb, amit egy idojarast nem figyelo idozito elerhet)

Csak a Python szabvanykonyvtarat hasznalja (nincs pip install).
Adatforras: Open-Meteo (https://open-meteo.com), CC BY 4.0.
"""
import argparse
import csv
import datetime as dt
import json
import math
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ----------------------------------------------------------------------------
# Beallitasok
# ----------------------------------------------------------------------------
CONFIG = {
    "lat": 47.53,                  # Debrecen
    "lon": 21.63,
    "tz": "Europe/Budapest",
    # gyep es talaj
    "kc": 0.80,                    # hidegturo gyep (UC ANR; Meyer et al., 1985)
    "p": 0.40,                     # hidegturo gyep kimerulesi hanyada (FAO-56, 22. tablazat)
    "theta_fc": 0.15,              # valyogos homok, szantofoldi vizkapacitas (FAO-56, 36. pelda)
    "theta_wp": 0.06,              # valyogos homok, hervadaspont (FAO-56, 36. pelda)
    "root_depth_m": 0.15,          # gyep gyokerzonaja: 10-15 cm-ig kell atnedvesiteni (Nebraska Extension)
    # ontozorendszer
    "precip_rate_mm_h": 10.0,      # Hunter PGP Ultra rotoros szorofej, ~10 mm/h
    "zones_m2": {"1": 124.0, "4": 124.0},    # gyepzonak, osszesen 248 m2
    "fixed_minutes": {"1": 15, "4": 30},     # a mostani napi idozites
    "rain_skip_mm": 5.0,           # ennyi vart esonel elhalasztja az ontozest
    "min_irrigation_mm": 2.0,      # ennel kevesebbert nem indit ontozest
    "state_file": "ontozes_allapot.json",
}

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = ("temperature_2m_max,temperature_2m_min,"
              "precipitation_sum,et0_fao_evapotranspiration")


def taw():
    """FAO-56, 82. egyenlet: TAW = 1000 (thetaFC - thetaWP) Zr, mm."""
    return 1000 * (CONFIG["theta_fc"] - CONFIG["theta_wp"]) * CONFIG["root_depth_m"]


def raw():
    return CONFIG["p"] * taw()


def area():
    return sum(CONFIG["zones_m2"].values())


# ----------------------------------------------------------------------------
# Adatletoltes
# ----------------------------------------------------------------------------
def fetch_daily(url, extra_params, retries=3, timeout=30):
    """Napi adatsor letoltese az Open-Meteo API-rol, sorok listajakent."""
    params = {
        "latitude": CONFIG["lat"],
        "longitude": CONFIG["lon"],
        "daily": DAILY_VARS,
        "timezone": CONFIG["tz"],
    }
    params.update(extra_params)
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(full, timeout=timeout) as resp:
                data = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                sys.exit(f"Nem sikerult letolteni az adatokat: {exc}")
            time.sleep(5 * attempt)
    return parse_daily(data)


def parse_daily(data):
    d = data["daily"]
    return [{
        "datum": day,
        "tmax": d["temperature_2m_max"][i],
        "tmin": d["temperature_2m_min"][i],
        "eso_mm": d["precipitation_sum"][i],
        "et0_mm": d["et0_fao_evapotranspiration"][i],
    } for i, day in enumerate(d["time"])]


# ----------------------------------------------------------------------------
# FAO-56 Hargreaves-ET0 (osszehasonlitashoz; csak homersekletbol szamol)
# ----------------------------------------------------------------------------
def extraterrestrial_radiation(lat_deg, doy):
    """Ra, MJ/m2/nap (FAO-56, 21. es 23-25. egyenlet)."""
    gsc = 0.0820
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
    delta = 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)
    ws = math.acos(-math.tan(phi) * math.tan(delta))
    return (24 * 60 / math.pi) * gsc * dr * (
        ws * math.sin(phi) * math.sin(delta)
        + math.cos(phi) * math.cos(delta) * math.sin(ws))


def hargreaves_et0(tmax, tmin, lat_deg, doy):
    """ET0, mm/nap (FAO-56, 52. egyenlet; 0,408: MJ/m2 -> mm)."""
    ra_mm = 0.408 * extraterrestrial_radiation(lat_deg, doy)
    tmean = (tmax + tmin) / 2
    return 0.0023 * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * ra_mm


# ----------------------------------------------------------------------------
# Talajvizmerleg
# ----------------------------------------------------------------------------
def effective_rain(rain, et0):
    """A kis esot a talajfelszin elparologtatja (FAO-56: P < 0,2 ET0 elhanyagolhato)."""
    return rain if rain >= 0.2 * et0 else 0.0


def ks(dr):
    """Vizstressz-egyutthato (FAO-56, 84. egyenlet)."""
    t, r = taw(), raw()
    return 1.0 if dr <= r else max(0.0, (t - dr) / (t - r))


def step(dr, et0, rain, irrigation):
    """Egy nap vizmerlege (hajnali ontozes, utana a napi parolgas es eso).

    Visszaadja: (uj Dr, tenyleges ETc, elszivargott viz mm, volt-e vizstressz).
    """
    d = dr - irrigation
    drained = max(0.0, -d)            # a gyokerzona befogadokepessege feletti viz elszivarog
    d = max(0.0, d)
    k = ks(d)
    etc = k * CONFIG["kc"] * et0
    d = d + etc - effective_rain(rain, et0)
    drained += max(0.0, -d)
    return min(max(0.0, d), taw()), etc, drained, k < 1.0


def zone_minutes(mm):
    """Ennyi perc kell zonankent, hogy a gyep `mm` vizet kapjon."""
    return {z: round(mm / CONFIG["precip_rate_mm_h"] * 60) for z in CONFIG["zones_m2"]}


def strategies():
    """(nev, terulet m2) parok: az okos es a hangolt mindket zonara ugyanazt adja,
    a fix idozites zonankent mas, ezert zonankent kulon fut."""
    out = [("okos", area()), ("hangolt", area())]
    out += [(f"fix{z}", CONFIG["zones_m2"][z]) for z in CONFIG["fixed_minutes"]]
    return out


def fixed_mm(zone):
    return CONFIG["precip_rate_mm_h"] * CONFIG["fixed_minutes"][zone] / 60


def tuned_mm_by_month(rows):
    """Havi atlagos netto vizigeny (Kc*ET0 - hasznos eso), mm/nap."""
    acc = {}
    for r in rows:
        m = r["datum"][:7]
        need = CONFIG["kc"] * r["et0_mm"] - effective_rain(r["eso_mm"], r["et0_mm"])
        s, n = acc.get(m, (0.0, 0))
        acc[m] = (s + need, n + 1)
    return {m: max(0.0, s / n) for m, (s, n) in acc.items()}


def simulate(rows):
    rows = [r for r in rows if r["et0_mm"] is not None and r["eso_mm"] is not None]
    r_thr = raw()
    tuned = tuned_mm_by_month(rows)
    strat = strategies()
    dr = {name: 0.0 for name, _ in strat}
    out = []
    for r in rows:
        doy = dt.date.fromisoformat(r["datum"]).timetuple().tm_yday
        row = dict(r)
        row["et0_hargreaves_mm"] = round(hargreaves_et0(r["tmax"], r["tmin"], CONFIG["lat"], doy), 2)
        row["eso_hasznos_mm"] = round(effective_rain(r["eso_mm"], r["et0_mm"]), 2)
        for name, a in strat:
            if name == "okos":
                # hajnalban dont az elozo nap vegi hiany alapjan; a mai
                # elorejelzest a visszamenoleges futasban a mai tenyleges eso adja
                irr = 0.0
                if (dr[name] >= r_thr and dr[name] >= CONFIG["min_irrigation_mm"]
                        and r["eso_mm"] < CONFIG["rain_skip_mm"]):
                    irr = dr[name]
            elif name == "hangolt":
                irr = tuned[r["datum"][:7]]
            else:
                irr = fixed_mm(name[3:])
            dr[name], etc, drained, stress = step(dr[name], r["et0_mm"], r["eso_mm"], irr)
            row[f"ontozes_{name}_mm"] = round(irr, 2)
            row[f"ontozes_{name}_l"] = round(irr * a, 1)
            row[f"hiany_{name}_mm"] = round(dr[name], 2)
            row[f"elszivargas_{name}_l"] = round(drained * a, 1)
            row[f"stressz_{name}"] = int(stress)
        out.append(row)
    return out


def summarize(sim):
    res = {
        "idoszak": f'{sim[0]["datum"]} - {sim[-1]["datum"]}',
        "napok": len(sim),
        "gyep_m2": area(),
        "TAW_mm": round(taw(), 1),
        "RAW_kuszob_mm": round(raw(), 1),
        "et0_osszeg_mm": round(sum(r["et0_mm"] for r in sim), 1),
        "et0_hargreaves_osszeg_mm": round(sum(r["et0_hargreaves_mm"] for r in sim), 1),
        "eso_osszeg_mm": round(sum(r["eso_mm"] for r in sim), 1),
        "eso_hasznos_osszeg_mm": round(sum(r["eso_hasznos_mm"] for r in sim), 1),
        "gyep_vizigeny_kc_et0_mm": round(sum(CONFIG["kc"] * r["et0_mm"] for r in sim), 1),
    }
    for name, _ in strategies():
        zones = 1 if name.startswith("fix") else len(CONFIG["zones_m2"])
        mm = sum(r[f"ontozes_{name}_mm"] for r in sim)
        res[name] = {
            "ontozes_l": round(sum(r[f"ontozes_{name}_l"] for r in sim)),
            "szivattyu_ora": round(mm / CONFIG["precip_rate_mm_h"] * zones, 1),
            "ontozesi_napok": sum(1 for r in sim if r[f"ontozes_{name}_mm"] > 0),
            "elszivargas_l": round(sum(r[f"elszivargas_{name}_l"] for r in sim)),
            "stresszes_napok": sum(r[f"stressz_{name}"] for r in sim),
        }
    fix = [n for n, _ in strategies() if n.startswith("fix")]
    res["fix_osszesen"] = {
        "ontozes_l": sum(res[n]["ontozes_l"] for n in fix),
        "szivattyu_ora": round(sum(res[n]["szivattyu_ora"] for n in fix), 1),
        "elszivargas_l": sum(res[n]["elszivargas_l"] for n in fix),
    }
    smart = res["okos"]["ontozes_l"]
    for base, val in (("fix", res["fix_osszesen"]["ontozes_l"]), ("hangolt", res["hangolt"]["ontozes_l"])):
        res[f"megtakaritas_{base}hez_kepest_l"] = val - smart
        res[f"megtakaritas_{base}hez_kepest_szazalek"] = round(100 * (val - smart) / val, 1) if val else None
    res["beallitasok"] = CONFIG
    return res


def monthly(sim):
    names = [n for n, _ in strategies()]
    out = {}
    for r in sim:
        m = r["datum"][:7]
        o = out.setdefault(m, {"honap": m, "et0_mm": 0.0, "eso_mm": 0.0,
                               **{f"{n}_l": 0.0 for n in names},
                               "okos_napok": 0,
                               **{f"stressz_{n}": 0 for n in names}})
        o["et0_mm"] += r["et0_mm"]
        o["eso_mm"] += r["eso_mm"]
        o["okos_napok"] += int(r["ontozes_okos_mm"] > 0)
        for n in names:
            o[f"{n}_l"] += r[f"ontozes_{n}_l"]
            o[f"stressz_{n}"] += r[f"stressz_{n}"]
    return [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in o.items()}
            for o in out.values()]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ----------------------------------------------------------------------------
# Uzemmodok
# ----------------------------------------------------------------------------
def run_backfill(rows):
    write_csv("ontozes_idojaras.csv", rows)
    sim = simulate(rows)
    write_csv("ontozes_szimulacio.csv", sim)
    write_csv("ontozes_havi.csv", monthly(sim))
    summary = summarize(sim)
    pathlib.Path("ontozes_osszegzes.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "beallitasok"},
                     ensure_ascii=False, indent=2))


def cmd_backfill(args):
    if args.from_file:   # korabban letoltott (archivalt) valasz ujrafeldolgozasa
        data = json.loads(pathlib.Path(args.from_file).read_text(encoding="utf-8"))
        rows = [r for r in parse_daily(data) if args.start <= r["datum"] <= args.end]
    else:
        rows = fetch_daily(ARCHIVE_URL, {"start_date": args.start, "end_date": args.end})
    run_backfill(rows)


def cmd_today(_args):
    # az allapotfajl a szkript mellett van, barhonnan inditjak (pl. Home Assistant shell_command)
    state_path = pathlib.Path(CONFIG["state_file"])
    if not state_path.is_absolute():
        state_path = pathlib.Path(__file__).resolve().with_name(CONFIG["state_file"])
    state = {"hiany_mm": 0.0, "utolso_nap": None, "dontes": None}
    if state_path.exists():
        state.update(json.loads(state_path.read_text(encoding="utf-8")))
    today = dt.date.today().isoformat()
    if state["dontes"] and state["dontes"]["datum"] == today:
        print(json.dumps(state["dontes"], ensure_ascii=False))   # ma mar dontott
        return
    # a Home Assistant 60 s utan leallitja a parancsot, ezert rovidebb idokorlat
    rows = fetch_daily(FORECAST_URL, {"past_days": 7, "forecast_days": 1}, retries=2, timeout=15)
    for r in rows:   # a kimaradt mult napok potlasa
        if r["datum"] >= today or r["et0_mm"] is None:
            continue
        if state["utolso_nap"] is None or r["datum"] > state["utolso_nap"]:
            state["hiany_mm"], _, _, _ = step(state["hiany_mm"], r["et0_mm"], r["eso_mm"] or 0.0, 0.0)
            state["utolso_nap"] = r["datum"]
    expected = next((r["eso_mm"] or 0.0 for r in rows if r["datum"] == today), 0.0)
    irr, reason = 0.0, "nincs eleg vizhiany"
    if state["hiany_mm"] >= raw():
        if expected >= CONFIG["rain_skip_mm"]:
            reason = f"eso varhato ({expected:.1f} mm), elhalasztva"
        else:
            irr, reason = state["hiany_mm"], "ontozes"
    decision = {
        "datum": today,
        "hiany_mm": round(state["hiany_mm"], 1),
        "kuszob_mm": round(raw(), 1),
        "varhato_eso_mm": round(expected, 1),
        "ontozes_mm": round(irr, 1),
        "ontozes_l": round(irr * area()),
        "percek": zone_minutes(irr),
        "indok": reason,
    }
    state["hiany_mm"] = max(0.0, state["hiany_mm"] - irr)
    state["dontes"] = decision
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(decision, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description="Idojaras-alapu gyepontozes (FAO-56 vizmerleg)")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="szezon letoltese es szimulacioja")
    b.add_argument("--start", required=True, help="kezdo nap, pl. 2026-04-01")
    b.add_argument("--end", required=True, help="zaro nap, pl. 2026-10-15")
    b.add_argument("--from-file", help="letoltes helyett egy elmentett Open-Meteo JSON-valasz")
    b.set_defaults(func=cmd_backfill)
    t = sub.add_parser("today", help="mai ontozesi dontes JSON-ban")
    t.set_defaults(func=cmd_today)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
