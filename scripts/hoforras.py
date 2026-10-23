#!/usr/bin/env python3
"""
hoforras.py - koltsegalapu hoforras-valasztas (szakdolgozat 8.5)

Ket uzemmod:

  python3 hoforras.py most < allapot.json     (vagy: hoforras.py most '{...}')
      A Home Assistant altal atadott pillanatnyi allapotbol (JSON a stdin-en)
      kiszamolja, mennyibe kerul most 1 kWh ho az egyes hoforrasokbol, es
      egy sor JSON-t ir ki a legolcsobb hoforrassal. A Home Assistant ezt
      orankent futtatja (6.7), es a valasz alapjan engedelyezi a hoforrast.

  python3 hoforras.py tabla
      A dolgozat 8.3. tablazata: 1 kWh ho ara hoforrasonkent a harom
      villanyar-helyzetben, valamint a klima es a tuzifa fedezeti COP-ja.

A modszer:
  1 kWh ho ara            c_i = p_i / eta_i          [Ft/kWh ho]
  klima                   p = hatar-villanyar, eta = COP (a kulso homerseklettol fugg)
  villanyradiator         p = hatar-villanyar, eta = 1
  tuzifa (kazan)          p = fa ara / fa energiatartalma, eta = kazanhatasfok
  puffer (napkollektor)   0, ha eleg meleg a futeskorhoz

  A hatar-villanyar (a kovetkezo kWh ara) az elszamolastol fugg:
    szaldo:  a varhato eves netto fogyasztas N (vetelezes - betaplalas, aug. 1. - jul. 31.)
             N <= 0          -> a tobbletet ugyis alacsony aron vennek at: betaplalasi ar
             0 < N <= 2523   -> kedvezmenyes ar
             N > 2523        -> savhatar feletti ar
    brutto:  ha eppen napelemes tobblet van -> betaplalasi ar (ennyit veszit, ha nem tapl. be)
             kulonben a vasarlasi ar a sav szerint

Csak a Python szabvanykonyvtarat hasznalja.
"""
import json
import sys

CONFIG = {
    # villamos energia (2.3; Bank360, e.n.)
    "ar_kedvezmenyes": 36.9,        # Ft/kWh, A1 tarifa, savhataron belul
    "ar_piaci": 70.104,             # Ft/kWh, savhatar felett
    "savhatar_kwh": 2523,           # kWh/ev, felhasznalasi helyenkent
    "ar_betaplalas": 5.0,           # Ft/kWh, atveteli ar (REKK, 2023: kb. 5 Ft/kWh)
    # klima: A+ energiaosztaly also hatara, atlagos futesi idenyre (626/2011/EU rendelet)
    "klima_scop": 4.0,
    # ha ismert a gyartoi adattabla ket pontja, a COP a kulso homerseklettol fugg:
    # {"7": 4.0, "-7": 2.6} formaban; ures eseten a SCOP-pal szamol
    "klima_cop_pontok": {},
    # tuzifa: kemenylombos, hasogatott (penzcentrum.hu, 2025), tolgy/akac 2940 kWh/m3 (NEBIH, e.n.)
    "fa_ar_ft_m3": 44000,
    "fa_energia_kwh_m3": 2940,
    "kazan_hatasfok": 0.75,
    # puffer: ennyi fok felett kepes futeni a futeskor
    "puffer_min_c": 45.0,
}

FORRASOK = ("puffer", "klima", "radiator", "fa")


def cop(kulso_c):
    """A klima COP-ja: ket adattablapont kozott linearis, kivul a szelsoertek."""
    p = {float(k): v for k, v in CONFIG["klima_cop_pontok"].items()}
    if kulso_c is None or len(p) < 2:
        return CONFIG["klima_scop"]
    (t1, c1), (t2, c2) = sorted(p.items())[:2]
    t = min(max(kulso_c, t1), t2)
    return c1 + (c2 - c1) * (t - t1) / (t2 - t1)


def fa_ar_kwh():
    """1 kWh ho ara tuzifabol (kazanhatasfokkal)."""
    return CONFIG["fa_ar_ft_m3"] / (CONFIG["fa_energia_kwh_m3"] * CONFIG["kazan_hatasfok"])


def hatar_villanyar(elszamolas, netto_eves_kwh, pv_tobblet_w=0.0):
    """A kovetkezo kWh villany ara es a helyzet neve."""
    if elszamolas == "brutto":
        if pv_tobblet_w > 0:
            return CONFIG["ar_betaplalas"], "napelemes tobblet"
        # bruttonal a savhatar a teljes vetelezesre vonatkozik
        if netto_eves_kwh <= CONFIG["savhatar_kwh"]:
            return CONFIG["ar_kedvezmenyes"], "kedvezmenyes sav"
        return CONFIG["ar_piaci"], "savhatar felett"
    # szaldo
    if netto_eves_kwh <= 0:
        return CONFIG["ar_betaplalas"], "eves tobblet"
    if netto_eves_kwh <= CONFIG["savhatar_kwh"]:
        return CONFIG["ar_kedvezmenyes"], "kedvezmenyes sav"
    return CONFIG["ar_piaci"], "savhatar felett"


def hoarak(villanyar, kulso_c, puffer_c):
    """1 kWh ho ara hoforrasonkent (None: most nem elerheto)."""
    return {
        "puffer": 0.0 if (puffer_c is not None and puffer_c >= CONFIG["puffer_min_c"]) else None,
        "klima": villanyar / cop(kulso_c),
        "radiator": villanyar,
        "fa": fa_ar_kwh(),
    }


def dontes(allapot):
    """Allapot (a Home Assistanttol) -> dontes (JSON-ba irhato szotar).

    allapot kulcsai:
      elszamolas                 "szaldo" vagy "brutto"
      netto_kwh_ev_eleje_ota     vetelezes - betaplalas aug. 1. ota (szaldo)
                                 ill. a vetelezes aug. 1. ota (brutto)
      varhato_tovabbi_kwh        ugyanez a hatralevo idoszakra, elorejelzesbol
      kulso_c                    a kulso homerseklet (most vagy elorejelzes)
      puffer_c                   a puffer felso homerseklete
      pv_tobblet_w               a pillanatnyi betaplalas (brutto esetben szamit)
    """
    netto = float(allapot.get("netto_kwh_ev_eleje_ota", 0)) + float(allapot.get("varhato_tovabbi_kwh", 0))
    p, helyzet = hatar_villanyar(allapot.get("elszamolas", "szaldo"), netto,
                                 float(allapot.get("pv_tobblet_w", 0) or 0))
    arak = hoarak(p, allapot.get("kulso_c"), allapot.get("puffer_c"))
    # automatikusan kapcsolhato forrasok; a fat valakinek be kell gyujtania
    auto = {k: v for k, v in arak.items() if v is not None and k != "fa"}
    javaslat = min(auto, key=auto.get)
    sorrend = sorted((k for k, v in arak.items() if v is not None), key=lambda k: arak[k])
    return {
        "helyzet": helyzet,
        "varhato_eves_netto_kwh": round(netto),
        "villanyar_ft_kwh": round(p, 2),
        "klima_cop": round(cop(allapot.get("kulso_c")), 2),
        "hoar_ft_kwh": {k: (round(v, 1) if v is not None else None) for k, v in arak.items()},
        "sorrend": sorrend,
        "javaslat": javaslat,
        "befutes_javasolt": sorrend[0] == "fa",
    }


def tabla():
    helyzetek = [("Tobblet / betaplalasi ar", CONFIG["ar_betaplalas"]),
                 ("Kedvezmenyes sav", CONFIG["ar_kedvezmenyes"]),
                 ("Savhatar felett", CONFIG["ar_piaci"])]
    sorok = []
    for nev, p in helyzetek:
        a = hoarak(p, None, 60)
        sorok.append({"helyzet": nev, "villanyar": p,
                      **{k: round(v, 1) for k, v in a.items()}})
    fa = fa_ar_kwh()
    return {
        "tabla_ft_per_kwh_ho": sorok,
        "fa_ft_per_kwh_ho": round(fa, 2),
        # ennel kisebb COP mellett a fa olcsobb a klimanal
        "fedezeti_cop": {nev: round(p / fa, 2) for nev, p in helyzetek},
        "beallitasok": CONFIG,
    }


def main(argv):
    if len(argv) < 2 or argv[1] not in ("most", "tabla"):
        sys.exit("Hasznalat: hoforras.py most ['{json}'] < allapot.json  |  hoforras.py tabla")
    if argv[1] == "tabla":
        print(json.dumps(tabla(), ensure_ascii=False, indent=2))
        return
    try:
        # a Home Assistant shell_command-ja sablonnal nem tud csovezeteket hasznalni,
        # ezert az allapot argumentumkent is atadhato
        allapot = json.loads(argv[2]) if len(argv) > 2 else json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.exit(f"Hibas bemenet: {exc}")
    print(json.dumps(dontes(allapot), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
