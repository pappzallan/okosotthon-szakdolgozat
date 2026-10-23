# Okosotthon környezeti monitorozó és prediktív vezérlő rendszer

A Debreceni Egyetem Informatikai Karán készült szakdolgozat (gazdaságinformatikus BSc, 2026) saját fejlesztésű programjai és konfigurációi.

**Szerző:** Papp Zalán

A tároló egy működő családi házas okosotthon-rendszer saját részeit tartalmazza. A rendszer központja a [Home Assistant](https://www.home-assistant.io/), az eszközök [ESPHome](https://esphome.io/) firmware-rel futnak.

> A tároló **nem tartalmaz** kulcsot, jelszót vagy tokent. Ezek a `secrets.yaml` fájlban, illetve külön kulcsfájlban vannak, amelyeket a `.gitignore` kizár. Minta: [`esphome/secrets.yaml.example`](esphome/secrets.yaml.example).

## Tartalom

| Fájl | Mit csinál | Dolgozat |
| --- | --- | --- |
| `scripts/wunderground.sh` | A Weather Underground személyes időjárás-állomás lekérdezése; egy sorban adja a Home Assistantnak, és napi összesítőt ír CSV-be | 6.6 |
| `scripts/ontozes.py` | Időjárás-alapú öntözési döntés FAO-56 talajvízmérleggel (Open-Meteo / ERA5 adatokból); `backfill` és `today` mód | 6.6, 8.3 |
| `scripts/hoforras.py` | Költségalapú hőforrás-választás (puffer, klíma, villanyradiátor, tűzifa) a szaldós/bruttó elszámolás és a sávhatár szerint; `most` és `tabla` mód | 6.6, 8.5 |
| `esphome/ontozo-rendszer.yaml` | Az öntözővezérlő (ESP32, 6 zóna + szivattyú) javított konfigurációja: várakozási sor, zónaretesz, időszinkron-ellenőrzés | 6.5, F.7 |
| `esphome/puffer-modul-reszlet.yaml` | A puffer- és kazánmodul (ESP8266) javasolt módosítása: biztonságos irányú relélogika, jelszavas webfelület (részlet) | 6.4, F.2 |
| `homeassistant/packages/futes.yaml` | „Ha süt a nap, ne induljon a fűtés”: a fűtés halasztása napelemes termelés-előrejelzés alapján | 8.1 |
| `homeassistant/packages/hoforras.yaml` | A `hoforras.py` óránkénti futtatása és a kiválasztott hőforrás bekapcsolása | 8.5 |
| `homeassistant/packages/ontozes.yaml` | Az öntözési döntés napi futtatása és átadása az öntözővezérlőnek | 8.3 |

## Használat röviden

**Szkriptek** – csak a Python szabványkönyvtára, illetve `bash`, `curl` és `jq` kell:

```bash
python3 scripts/hoforras.py tabla                # a 8.3. táblázat számai
python3 scripts/ontozes.py today                 # mai öntözési döntés (JSON)
WU_STATION=<allomas> WU_KEY_FILE=~/.config/wunderground/apikey bash scripts/wunderground.sh
```

**Home Assistant-csomagok** – a `configuration.yaml`-ben:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

A csomagokban szereplő entitásneveket (például `sensor.deli_szoba_homerseklet`) a saját rendszer neveire kell cserélni. A `hoforras.yaml` a `futes.yaml`-lel együtt használandó.

**ESPHome** – a `secrets.yaml.example` alapján készíts `secrets.yaml`-t az `esphome` mappába, minden eszközhöz új API-kulccsal és jelszóval.

> **Biztonság:** a kazánt vezérlő konfiguráció élesítése előtt próbapadon kell tesztelni (dolgozat 5.6). A 230 V-os áramkörök szerelése szakember feladata.

## Felhasznált nyílt források

- Allen, R. G. et al. (1998): *Crop evapotranspiration* (FAO Irrigation and Drainage Paper 56)
- Open-Meteo időjárási API (CC BY 4.0)
- Home Assistant, ESPHome dokumentáció
