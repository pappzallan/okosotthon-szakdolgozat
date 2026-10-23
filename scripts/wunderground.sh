#!/usr/bin/env bash
# wunderground.sh - Weather Underground szemelyes idojaras-allomas (PWS) lekerdezese
#
# Kimenet (stdout): egy sor, 29 ertek ketsponttal elvalasztva, a legutobbi
#   5 perces megfigyelesbol - ugyanazok a mezok, mint a korabbi valtozatban,
#   igy a Home Assistant command_line szenzora valtozatlanul olvashatja.
# Mellekhatas: napi osszesito sor a CSV naplofajlba (napi max/min, napi
#   csapadek, kozelito sugarzasosszeg). Egy naphoz egy sor tartozik, ami
#   napkozben frissul, igy a nap vegere a teljes napot tartalmazza.
#
# Az API-kulcs NINCS a szkriptben: kulon fajlbol olvassa (chmod 600), es
# nem kerul a parancssorba sem (a curl a konfiguraciot a stdin-rol kapja,
# igy a kulcs a folyamatlistaban sem latszik).
#
# Fuggosegek: bash, curl, jq
#
# Home Assistant (configuration.yaml) - pelda:
#   command_line:
#     - sensor:
#         name: "Idojaras-allomas"
#         command: "WU_STATION=<allomas-azonosito> WU_KEY_FILE=/config/.wu_apikey WU_LOG_FILE=/config/wunderground_napi.csv bash /config/scripts/wunderground.sh"
#         scan_interval: 300        # az allomas 5 percenkent kuld adatot; az alapertelmezett 60 s folosleges
#         command_timeout: 60       # alapertelmezes 15 s, a letoltes ujraprobalkozassal ennel tovabb tarthat
#         value_template: "{{ value.split(':')[8] }}"   # 9. mezo: atlaghomerseklet
#         unit_of_measurement: "°C"
# A kulcsfajl (/config/.wu_apikey) keruljon a .gitignore-ba, GitHubra soha.
# Hiba eseten (nem nulla kilepesi kod) a Home Assistant megtartja az utolso
# erteket es naplozza a hibat, ezert a frissules idejet is erdemes figyelni.
set -euo pipefail

STATION="${WU_STATION:?Add meg az allomas azonositojat: WU_STATION=...}"
KEY_FILE="${WU_KEY_FILE:-$HOME/.config/wunderground/apikey}"
LOG_FILE="${WU_LOG_FILE:-$HOME/wunderground_napi.csv}"
API="https://api.weather.com/v2/pws/observations/all/1day"

if [[ ! -r "$KEY_FILE" ]]; then
    echo "Hianyzik az API-kulcs fajl: $KEY_FILE" >&2
    exit 1
fi
API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"

# --- letoltes: 10 s idokorlat, 2 ujraprobalkozas (osszesen legfeljebb ~40 s) ----
json="$(printf 'url = "%s?stationId=%s&format=json&units=m&numericPrecision=decimal&apiKey=%s"\n' \
            "$API" "$STATION" "$API_KEY" \
        | curl --silent --show-error --fail --max-time 10 \
               --retry 2 --retry-delay 3 --config -)"

if ! jq -e '.observations | length > 0' >/dev/null <<<"$json"; then
    echo "Az allomas nem kuldott megfigyelest (${STATION})" >&2
    exit 1
fi

# --- pillanatnyi ertekek (a legutobbi 5 perces megfigyeles) ---------------------
jq -r '
  .observations[-1] as $o | $o.metric as $m
  | [ $o.solarRadiationHigh, $o.uvHigh, $o.winddirAvg,
      $o.humidityHigh, $o.humidityLow, $o.humidityAvg,
      $m.tempHigh, $m.tempLow, $m.tempAvg,
      $m.windspeedHigh, $m.windspeedLow, $m.windspeedAvg,
      $m.windgustHigh, $m.windgustLow, $m.windgustAvg,
      $m.dewptHigh, $m.dewptLow, $m.dewptAvg,
      $m.windchillHigh, $m.windchillLow, $m.windchillAvg,
      $m.heatindexHigh, $m.heatindexLow, $m.heatindexAvg,
      $m.pressureMax, $m.pressureMin, $m.pressureTrend,
      $m.precipRate, $m.precipTotal ]
  | map(if . == null then "" else tostring end)
  | join(":")' <<<"$json"

# --- napi osszesito a naplofajlba ----------------------------------------------
# sugarzas: sum(solarRadiationHigh * meresi intervallum) -> MJ/m2 (felso becsles,
# mert az intervallum csucserteket szorozza)
daily="$(jq -r '
  .observations as $obs
  | def nums(f): [ $obs[] | f | numbers ];
    def avg(a): if (a | length) > 0 then (a | add / length * 10 | round / 10) else null end;
  [ ($obs[-1].obsTimeLocal[0:10]),
    (nums(.metric.tempHigh) | max),
    (nums(.metric.tempLow)  | min),
    avg(nums(.metric.tempAvg)),
    avg(nums(.humidityAvg)),
    avg(nums(.metric.windspeedAvg)),
    (nums(.metric.precipTotal) | max),
    ([ range(1; $obs | length) as $i
       | ($obs[$i].epoch - $obs[$i-1].epoch) * ($obs[$i].solarRadiationHigh // 0) ]
       | add // 0 | . / 1e6 * 100 | round / 100),
    ($obs | length) ]
  | map(if . == null then "" else tostring end)
  | join(",")' <<<"$json")"
day="${daily%%,*}"

(
    if command -v flock >/dev/null 2>&1; then flock -w 10 9 || true; fi
    tmp="$(mktemp "${LOG_FILE}.XXXXXX")"
    if [[ -s "$LOG_FILE" ]]; then
        grep -v "^${day}," "$LOG_FILE" > "$tmp" || true
    else
        echo "datum,tmax_c,tmin_c,tatlag_c,paratartalom_atlag,szel_atlag_kmh,csapadek_mm,sugarzas_mj_m2,meresek" > "$tmp"
    fi
    echo "$daily" >> "$tmp"
    mv "$tmp" "$LOG_FILE"
) 9>"${LOG_FILE}.lock"
