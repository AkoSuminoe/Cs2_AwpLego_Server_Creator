<div align="center">

# CS2 Automated Server Setup & Management Tool

**Deploy a fully configured Counter-Strike 2 dedicated server — with mods, plugins, live RCON control, automatic backups, and deterministic restore — in a single command.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/style-PEP8-black)](https://peps.python.org/pep-0008/)
[![Async](https://img.shields.io/badge/async-asyncio-purple)](https://docs.python.org/3/library/asyncio.html)

</div>

---

## What Is This?

Setting up a CS2 dedicated server is a multi-hour chore: download SteamCMD, install 30 GB of game files, manually patch `gameinfo.gi`, unzip Metamod and CounterStrikeSharp into the right folder structure, then hunt for each plugin's GitHub release. One wrong folder and nothing loads.

This tool automates the entire pipeline — **idempotently**. Run it once to install. Run it again and every completed step is detected and skipped. Interrupted? Re-run and it picks up where it left off.

Phase 2 goes further: every plugin install is preceded by an **atomic ZIP snapshot** of the addon tree. If the plugin breaks the server, one call restores the last known-good state. A **deterministic lock file** (`cs2-plugins.lock`) pins every installed plugin to its exact release URL, enabling a byte-for-byte server clone on any machine with `--restore`. A **pure-asyncio RCON client** lets you manage the live server — add admins, change maps, kick players — without restarting.

---

## Key Features

| Feature | Description |
|---|---|
| **One-command install** | SteamCMD → CS2 → Metamod → CSSharp → plugins, fully automated |
| **Live progress bars** | SteamCMD stdout parsed line-by-line via async subprocess; download % displayed in real time |
| **Smart Unzip engine** | Handles every GitHub ZIP layout without hardcoded assumptions |
| **Dynamic plugin UI** | Paste any `owner/repo` GitHub slug; latest release fetched, classified, and installed |
| **Idempotent pipeline** | Filesystem predicates are ground truth; safe to re-run at any time |
| **Atomic state files** | `install_state.json` and `cs2-plugins.lock` written with `os.replace()` — never corrupt on crash |
| **Snapshot & rollback** | ZIP backup taken before every plugin install; auto-rollback on failure |
| **Plugin lock file** | Every install pinned by version, commit ref, and exact download URL |
| **`--restore` mode** | Recreate a server's exact plugin set on any machine from `cs2-plugins.lock` |
| **Async RCON client** | Source RCON over pure asyncio — add admins, change maps, kick/ban, broadcast |

---

## Architecture

```
cs2_server_creator/
│
├── main.py                   ← Thin async orchestrator (phases 0-9, no business logic)
│
├── core/
│   ├── mod_manager.py        ← GitHub API + Smart Unzip engine  ★ centrepiece
│   ├── snapshot.py           ← Atomic ZIP backup + rollback engine
│   ├── lock_manager.py       ← cs2-plugins.lock read/write (StateManager pattern)
│   ├── rcon_manager.py       ← Source RCON wire protocol over asyncio TCP
│   ├── steamcmd_wrapper.py   ← Async subprocess wrapper, progress regex parser
│   ├── config_patcher.py     ← Idempotent gameinfo.gi patch, bat/cfg writer
│   └── validator.py          ← Filesystem predicates + install_state.json state machine
│
├── cli/
│   └── app.py                ← Rich UI: banner, prompts, plugin manager, progress bars
│
├── models/
│   └── schemas.py            ← Shared dataclasses and Enums (ZipCase, SnapshotMeta, …)
│
└── utils/
    └── http_client.py        ← httpx.AsyncClient factory, tenacity retry decorator
```

### Installation Pipeline (Phases 0 – 9)

```
Phase 0  Collect all user input (GSLT, auth key, IP, RCON password, plugins)
   │
Phase 2  Download SteamCMD          ← skip if steamcmd.exe exists
   │
Phase 3  Install CS2 Server         ← skip if cs2.exe exists
   │                                   async for event in install_cs2():
   │                                       progress_bar.update(event.percent)
Phase 4  Install Metamod            ← skip if addons/metamod/ exists
   │
Phase 5  Install CounterStrikeSharp ← skip if addons/counterstrikesharp/ exists
   │
Phase 6  Patch gameinfo.gi          ← skip if "csgo/addons/metamod" already in file
   │
Phase 7  Write server configs        ← always (start_server.bat + server.cfg)
   │
Phase 8  Install user plugins        ← per-plugin:
   │       ├─ take_snapshot(csgo_dir)           before install
   │       ├─ install_mod(repo, target_dir)     GitHub API + Smart Unzip
   │       ├─ lock_mgr.record(entry)            pin to cs2-plugins.lock
   │       └─ snapshot.rollback(snap)           on failure — self-healing
   │
Phase 9  Summary table
```

---

## Smart Unzip Engine

Every GitHub plugin release uses a different ZIP layout. The engine inspects `namelist()` **before extracting a single byte**, classifies the structure, and applies the correct strategy.

```
ZIP received
     │
     ▼
Is "addons/" a top-level entry?
     │
    YES ──────────────────────────────► CASE A: DIRECT
     │                                  Extract as-is → game/csgo/
     │                                  Used by: Metamod, CounterStrikeSharp
    NO
     │
     ▼
Is there exactly ONE real directory at root?
     │
    YES ──────────────────────────────► CASE B: WRAPPER FLATTEN
     │                                  Strip "PluginName-v1.2/" prefix from every path
     │                                  Write via zip.open() pipe — no temp directory
     │                                  Used by: most GitHub plugin releases
    NO
     │
     ▼
Are there .dll files directly at root?
     │
    YES ──────────────────────────────► CASE C: FLAT DLL
     │                                  Extract directly into plugin target folder
     │                                  Used by: simple single-file plugins
    NO
     │
     ▼
     AMBIGUOUS ──────────────────────► Raise UnrecognizedZipStructureError
                                        (includes full namelist in message)
```

> **Why no intermediate temp directory for WRAPPER FLATTEN?**
> Each member is read via `zipfile.open(info)` and piped directly to the destination `Path.open('wb')`. This halves disk I/O and is faster on large plugin packages.

---

## Snapshot & Rollback (Self-Healing)

Before every plugin install, the engine takes an **atomic ZIP snapshot** of `game/csgo/addons/` and `game/csgo/cfg/`. If the install fails — broken archive, bad ZIP structure, network drop — the system rolls back to the pre-install state in under a second.

```
Phase 8: plugin install loop
     │
     ├─ take_snapshot(csgo_dir, .snapshots/, label="before_PluginName")
     │     ZIP of addons/ + cfg/ → .snapshots/20250705T123456_before_PluginName.zip
     │     Companion .json written atomically (os.replace)
     │
     ├─ install_mod(...)   ──► SUCCESS → record to cs2-plugins.lock
     │
     └─ install_mod(...)   ──► FAILURE
           │
           └─ snapshot.rollback(snap, csgo_dir)
                 shutil.rmtree(addons/, cfg/)
                 zipfile.extractall(csgo_dir)
                 console: "Rolled back to 20250705T123456_before_PluginName"
```

Only the last **5 snapshots** are kept on disk. `cleanup_old_snapshots()` runs automatically after each new snapshot.

---

## Plugin Lock File

Every successful plugin install is recorded in `cs2-plugins.lock` with its exact GitHub release metadata:

```json
{
  "schema_version": 1,
  "entries": {
    "cssjunkie/SomePlugin": {
      "owner": "cssjunkie",
      "repo": "SomePlugin",
      "version": "v1.4.2",
      "commit_ref": "main",
      "download_url": "https://github.com/cssjunkie/SomePlugin/releases/download/v1.4.2/SomePlugin.zip",
      "asset_keyword": null,
      "installed_at": "2025-07-05T12:34:56Z"
    }
  }
}
```

### Restoring a Server from the Lock File

```bash
python main.py --restore
```

The `--restore` flag bypasses the installer entirely. It reads every entry from `cs2-plugins.lock`, downloads each plugin from its **pinned URL** (no GitHub API lookup), and reinstalls them into the correct directories. The result is a byte-for-byte identical plugin set — on any machine, any VPS, any Docker container.

---

## RCON Manager

`core/rcon_manager.py` implements the **Source RCON wire protocol** over a pure `asyncio` TCP socket — no external dependencies.

```python
async with RCONClient(host="127.0.0.1", port=27015, password="secret") as rcon:
    await rcon.add_admin("76561198000000000", "@css/root")
    await rcon.change_map("de_mirage")
    await rcon.broadcast("Server will restart in 5 minutes.")
    await rcon.kick_player("griefer", "Unsportsmanlike conduct")
    await rcon.ban_player("76561198000000001", duration_minutes=60)
```

The RCON password is set during the install prompt and injected into `start_server.bat` via `-rcon_password`. The server starts RCON-ready on first boot.

### Packet format

```
[int32 LE] size  = body_length + 10
[int32 LE] id    = arbitrary positive int (matched in response)
[int32 LE] type  = 3 (AUTH) | 2 (EXECCOMMAND) | 0 (RESPONSE_VALUE)
[bytes]    body  = UTF-8, null-terminated
[byte]     0x00  = empty string terminator
```

All I/O is wrapped in `asyncio.wait_for(..., timeout)`. Authentication failure raises `RCONAuthError`; connection failure raises `RCONConnectionError`.

---

## Why the Hybrid Model?

Most server setup guides are either:
- **Pure shell scripts** — fast but brittle, break on any version change
- **Docker images** — portable but opaque, hard to customise

This tool uses a **pre-seeding + async log-tailing hybrid**:

1. **Pre-seeding:** All configuration and plugin files are placed on disk *before* the server ever starts. The server boots correctly on its first launch, with no "restart to apply changes" loop.

2. **Async log tailing:** Python reads the subprocess stdout as an async stream. The event loop stays free, so the UI stays responsive, and progress values are parsed from real data — not fake timers.

3. **SaaS-ready design:** The same pipeline can run inside a Docker container on a VPS. Add a `--steam-id` flag, wire up a billing webhook, and you have a "one-click CS2 server" SaaS. The architecture was designed with this extension in mind from day one.

---

## Requirements

- Python 3.10+
- Windows (for the CS2 dedicated server binary; the Python code is OS-agnostic)
- A Steam [Game Server Login Token](https://steamcommunity.com/dev/managegameservers) (GSLT)
- A Steam [Web API Key](https://steamcommunity.com/dev/apikey)

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-username/cs2-server-creator
cd cs2-server-creator

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python main.py

# 4. Restore an existing plugin set on a new machine
python main.py --restore
```

The tool will prompt you for:
- Installation directory (default: `~/cs2_server`)
- Steam GSLT token and Web API key
- Server IP and default map
- **RCON password** (written into `start_server.bat`; used by `RCONClient`)
- Any CounterStrikeSharp plugins you want installed

---

## Adding Plugins at Runtime

In the plugin manager screen, enter any GitHub repo in either format:

```
owner/repo
https://github.com/owner/repo
```

The tool fetches the **latest release** automatically, takes a snapshot, classifies the ZIP layout, installs it, and records the exact version to `cs2-plugins.lock`:

```
game/csgo/addons/counterstrikesharp/plugins/<PluginName>/
```

No hardcoded plugin names. No hardcoded URLs. Zero manual file management. Full rollback on failure.

---

## Tech Stack

| Library | Role |
|---|---|
| [`httpx`](https://www.python-httpx.org/) | Async HTTP client; streams large downloads in chunks |
| [`rich`](https://rich.readthedocs.io/) | Terminal UI — progress bars, panels, tables, prompts |
| [`tenacity`](https://tenacity.readthedocs.io/) | Retry with exponential backoff for GitHub API calls |
| `asyncio` | Non-blocking subprocess for SteamCMD; async download streaming; RCON TCP client |
| `zipfile` | ZIP inspection, extraction, and snapshot archiving (stdlib) |
| `struct` | Source RCON binary packet encoding/decoding (stdlib) |

---

## Idempotency & State

Every step is gated by a **filesystem predicate** — the actual files on disk are always the source of truth.

```python
# gameinfo.gi is never patched twice
def is_gameinfo_patched(csgo_dir: Path) -> bool:
    content = (csgo_dir / "gameinfo.gi").read_text(encoding="utf-8")
    return "csgo/addons/metamod" in content
```

Two companion files track state:

| File | Purpose |
|---|---|
| `install_state.json` | Per-phase completion timestamps and versions |
| `cs2-plugins.lock` | Per-plugin exact version, commit ref, and download URL |

Both are written atomically with `os.replace()`. Delete either to force a re-check — nothing breaks.

---

## License

MIT — see [LICENSE](LICENSE).

---

---

<div align="center">

# CS2 Otomatik Sunucu Kurulum ve Yönetim Aracı

**Tek komutla, mod ve eklentileriyle, canlı RCON kontrolüyle, otomatik yedeklerle ve deterministik restore ile eksiksiz bir CS2 dedicated sunucu kur.**

</div>

---

## Bu Nedir?

CS2 dedicated sunucu kurmak saatler süren bir iş: SteamCMD indir, 30 GB oyun dosyasını yükle, `gameinfo.gi`'yi manuel düzenle, Metamod ve CounterStrikeSharp'ı doğru klasör yapısına çıkar, her eklentinin GitHub release'ini bul... Bir klasör yanlış giderse hiçbir şey çalışmaz.

Bu araç **tüm pipeline'ı otomatik ve idempotent** olarak yönetir. Bir kere çalıştır, kurulumu tamamlar. Tekrar çalıştır — tamamlanan adımları algılar ve atlar. Yarıda mı kesildi? Tekrar çalıştır, kaldığı yerden devam eder.

**Faz 2** ile araç artık sadece bir installer değil, kurumsal bir DevOps platformu: Her eklenti kurulumundan önce **atomik ZIP snapshot** alınır. Bir eklenti sunucuyu bozarsa, sistem tek çağrıyla son çalışan duruma döner. **Deterministik kilit dosyası** (`cs2-plugins.lock`) her eklentiyi tam sürüm URL'siyle sabitler — `--restore` ile herhangi bir makinede birebir aynı sunucu klonu saniyeler içinde kurulur. **Pure asyncio RCON istemcisi** ile canlı sunucuya bağlanıp admin ekleyebilir, harita değiştirebilir, oyuncu yönetimi yapabilirsin.

---

## Öne Çıkan Özellikler

| Özellik | Açıklama |
|---|---|
| **Tek komut kurulum** | SteamCMD → CS2 → Metamod → CSSharp → eklentiler, tam otomatik |
| **Canlı ilerleme çubukları** | SteamCMD stdout'u async subprocess ile satır satır okunur; indirme yüzdesi gerçek zamanlı gösterilir |
| **Akıllı Zip Çıkarma** | Her GitHub ZIP yapısını varsayım olmadan yönetir |
| **Dinamik eklenti arayüzü** | Herhangi bir `owner/repo` GitHub adresi gir; en son sürüm otomatik indirilip kurulur |
| **Idempotent pipeline** | Her adım çalışmadan önce dosya sistemi kontrol eder; istediğin zaman güvenle tekrar çalıştır |
| **Atomik durum dosyaları** | `install_state.json` ve `cs2-plugins.lock`, `os.replace()` ile yazılır — kilitlenme durumunda asla bozulmaz |
| **Snapshot ve Rollback** | Her eklenti kurulumundan önce ZIP yedeği alınır; kurulum başarısız olursa otomatik geri dönüş |
| **Eklenti kilit dosyası** | Her kurulum sürüm, commit ve tam URL ile `cs2-plugins.lock`'a kaydedilir |
| **`--restore` modu** | `cs2-plugins.lock` dosyasından sunucunun birebir aynı eklenti setini herhangi bir makinede kur |
| **Async RCON istemcisi** | Pure asyncio üzerinde Source RCON protokolü — admin ekle, harita değiştir, kick/ban, duyuru |

---

## Mimari

```
cs2_server_creator/
│
├── main.py                   ← İnce async orkestratör (faz 0-9, iş mantığı yok)
│
├── core/
│   ├── mod_manager.py        ← GitHub API + Akıllı Zip Çıkarma Motoru  ★ kalp
│   ├── snapshot.py           ← Atomik ZIP yedekleme + rollback motoru
│   ├── lock_manager.py       ← cs2-plugins.lock okuma/yazma (StateManager deseni)
│   ├── rcon_manager.py       ← asyncio TCP üzerinde Source RCON wire protokolü
│   ├── steamcmd_wrapper.py   ← Async subprocess sarmalayıcı, ilerleme regex ayrıştırıcı
│   ├── config_patcher.py     ← Idempotent gameinfo.gi yamalaması, bat/cfg yazıcı
│   └── validator.py          ← Dosya sistemi predikatları + install_state.json durum makinesi
│
├── cli/
│   └── app.py                ← Rich arayüz: banner, promptlar, eklenti yöneticisi, ilerleme çubukları
│
├── models/
│   └── schemas.py            ← Paylaşılan dataclass ve Enum'lar (ZipCase, SnapshotMeta, …)
│
└── utils/
    └── http_client.py        ← httpx.AsyncClient fabrikası, tenacity yeniden deneme dekoratörü
```

### Kurulum Pipeline'ı (Faz 0 – 9)

```
Faz 0  Tüm kullanıcı girdisi (GSLT, auth key, IP, RCON şifresi, eklentiler)
   │
Faz 2  SteamCMD İndir         ← steamcmd.exe varsa atla
   │
Faz 3  CS2 Sunucu Kur         ← cs2.exe varsa atla
   │                              async for event in install_cs2():
   │                                  progress_bar.update(event.percent)
Faz 4  Metamod Kur            ← addons/metamod/ varsa atla
   │
Faz 5  CounterStrikeSharp Kur ← addons/counterstrikesharp/ varsa atla
   │
Faz 6  gameinfo.gi Yama       ← "csgo/addons/metamod" zaten dosyada varsa atla
   │
Faz 7  Sunucu Konfigürasyonu  ← her zaman (start_server.bat + server.cfg)
   │
Faz 8  Kullanıcı Eklentileri  ← eklenti başına:
   │       ├─ take_snapshot(csgo_dir)          kurulumdan önce yedek al
   │       ├─ install_mod(repo, target_dir)    GitHub API + Akıllı Zip
   │       ├─ lock_mgr.record(entry)           cs2-plugins.lock'a kaydet
   │       └─ snapshot.rollback(snap)          hata durumunda — self-healing
   │
Faz 9  Özet tablosu
```

---

## Akıllı Zip Çıkarma Motoru

Her GitHub eklenti release'i farklı bir ZIP yapısı kullanır. Motor, `namelist()` ile yapıyı **tek bir byte çıkarmadan** inceler, yapıyı sınıflandırır ve doğru stratejiyi uygular.

```
ZIP alındı
     │
     ▼
"addons/" üst dizinde var mı?
     │
    EVET ─────────────────────────────► DURUM A: DİREKT
     │                                  Olduğu gibi çıkar → game/csgo/
     │                                  Kullananlar: Metamod, CounterStrikeSharp
    HAYIR
     │
     ▼
Kökde tam olarak BİR gerçek dizin var mı?
     │
    EVET ─────────────────────────────► DURUM B: SARMALAYICI DÜZLEŞTIRME
     │                                  "PluginAdi-v1.2/" önekini tüm yollardan sil
     │                                  zip.open() pipe'ı ile doğru yere yaz — ara klasör yok
     │                                  Kullananlar: çoğu GitHub plugin release'i
    HAYIR
     │
     ▼
Kökde .dll dosyaları var mı?
     │
    EVET ─────────────────────────────► DURUM C: DÜZLEMSEL DLL
     │                                  Doğrudan eklenti hedef klasörüne çıkar
    HAYIR
     │
     ▼
     BELIRSIZ ───────────────────────► UnrecognizedZipStructureError fırlat
                                        (tam namelist mesajda gösterilir)
```

---

## Snapshot ve Rollback (Self-Healing)

Her eklenti kurulumundan önce `game/csgo/addons/` ve `game/csgo/cfg/` klasörlerinin **atomik ZIP yedeği** alınır. Kurulum başarısız olursa — bozuk arşiv, hatalı ZIP yapısı, ağ kopması — sistem bir saniye içinde önceki çalışan duruma döner.

```
Faz 8: eklenti kurulum döngüsü
     │
     ├─ take_snapshot(csgo_dir, .snapshots/, label="before_PluginAdi")
     │     addons/ + cfg/ → .snapshots/20250705T123456_before_PluginAdi.zip
     │     Eşlik eden .json atomik yazma ile oluşturulur (os.replace)
     │
     ├─ install_mod(...)   ──► BAŞARILI → cs2-plugins.lock'a kaydet
     │
     └─ install_mod(...)   ──► BAŞARISIZ
           │
           └─ snapshot.rollback(snap, csgo_dir)
                 shutil.rmtree(addons/, cfg/)
                 zipfile.extractall(csgo_dir)
                 konsol: "Rolled back to 20250705T123456_before_PluginAdi"
```

Diskte yalnızca son **5 snapshot** saklanır. Her yeni snapshot sonrası `cleanup_old_snapshots()` otomatik çalışır.

---

## Eklenti Kilit Dosyası

Her başarılı eklenti kurulumu `cs2-plugins.lock` dosyasına tam GitHub release meta bilgisiyle kaydedilir:

```json
{
  "schema_version": 1,
  "entries": {
    "cssjunkie/SomePlugin": {
      "owner": "cssjunkie",
      "repo": "SomePlugin",
      "version": "v1.4.2",
      "commit_ref": "main",
      "download_url": "https://github.com/cssjunkie/SomePlugin/releases/download/v1.4.2/SomePlugin.zip",
      "asset_keyword": null,
      "installed_at": "2025-07-05T12:34:56Z"
    }
  }
}
```

### Kilit Dosyasından Sunucu Geri Yükleme

```bash
python main.py --restore
```

`--restore` bayrağı installer'ı tamamen atlar. `cs2-plugins.lock` içindeki her girdiyi okur, her eklentiyi **sabitlenmiş URL'den** indirir (GitHub API'ye gitmez) ve doğru dizinlere kurar. Sonuç: herhangi bir makine, VPS veya Docker container'da birebir aynı eklenti seti.

---

## RCON Yöneticisi

`core/rcon_manager.py`, **Source RCON wire protokolünü** pure `asyncio` TCP soketi üzerinde uygular — harici bağımlılık sıfır.

```python
async with RCONClient(host="127.0.0.1", port=27015, password="sifre") as rcon:
    await rcon.add_admin("76561198000000000", "@css/root")
    await rcon.change_map("de_mirage")
    await rcon.broadcast("Sunucu 5 dakika sonra yeniden başlayacak.")
    await rcon.kick_player("griefer", "Kural ihlali")
    await rcon.ban_player("76561198000000001", duration_minutes=60)
```

RCON şifresi kurulum sırasında girilen prompt'tan alınır ve `start_server.bat` içine `-rcon_password` parametresiyle enjekte edilir. Sunucu ilk açılışta RCON-hazır olarak başlar.

---

## Neden Hibrit Model?

Çoğu sunucu kurulum rehberi ya tamamen **shell script** (hızlı ama kırılgan) ya da tamamen **Docker** (taşınabilir ama opak). Bu araç **ön-besleme + async log okuma hibrit modelini** kullanır:

1. **Ön-Besleme (Pre-seeding):** Tüm konfigürasyon ve eklenti dosyaları sunucu hiç başlamadan diske yerleştirilir. Sunucu ilk açılışında hatasız başlar, "değişiklikleri uygulamak için yeniden başlat" döngüsü yok.

2. **Async Log Okuma:** Python, subprocess stdout'unu async stream olarak okur. Event loop meşgul olmaz, arayüz canlı kalır ve ilerleme değerleri sahte timer'dan değil gerçek veriden gelmektedir.

3. **SaaS'a Hazır Tasarım:** Aynı pipeline bir VPS üzerinde Docker içinde çalışabilir. `--steam-id` parametresi ve ödeme webhook'u ekle — "tek tıkla CS2 sunucu" SaaS'ına dönüşür. Mimari bu genişlemeyi ilk günden göz önünde bulundurarak tasarlandı.

---

## Gereksinimler

- Python 3.10+
- Windows (CS2 dedicated server binary için; Python kodu OS-agnostik)
- Steam [Game Server Login Token](https://steamcommunity.com/dev/managegameservers) (GSLT)
- Steam [Web API Key](https://steamcommunity.com/dev/apikey)

---

## Hızlı Başlangıç

```bash
# 1. Repoyu klonla
git clone https://github.com/kullanici-adi/cs2-server-creator
cd cs2-server-creator

# 2. Bağımlılıkları yükle
pip install -r requirements.txt

# 3. Çalıştır
python main.py

# 4. Mevcut eklenti setini yeni bir makinede geri yükle
python main.py --restore
```

Araç senden şunları isteyecek:
- Kurulum dizini (varsayılan: `~/cs2_server`)
- Steam GSLT token ve Web API anahtarı
- Sunucu IP adresi ve varsayılan harita
- **RCON şifresi** (`start_server.bat`'a yazılır; `RCONClient` tarafından kullanılır)
- Kurmak istediğin CounterStrikeSharp eklentileri

---

## Çalışma Zamanında Eklenti Ekleme

Eklenti yöneticisi ekranında herhangi bir GitHub reposunu şu formatlardan biriyle gir:

```
owner/repo
https://github.com/owner/repo
```

Araç en son release'i otomatik bulur, snapshot alır, ZIP yapısını sınıflandırır, şuraya kurar ve tam sürümü `cs2-plugins.lock`'a kaydeder:

```
game/csgo/addons/counterstrikesharp/plugins/<EklentiAdi>/
```

Sabit kodlanmış eklenti adı yok. Sabit kodlanmış URL yok. Sıfır manuel dosya yönetimi. Hata durumunda tam rollback.

---

## Teknoloji Yığını

| Kütüphane | Rol |
|---|---|
| [`httpx`](https://www.python-httpx.org/) | Async HTTP istemcisi; büyük dosyaları chunk'larla indirir |
| [`rich`](https://rich.readthedocs.io/) | Terminal arayüzü — ilerleme çubukları, paneller, tablolar, promptlar |
| [`tenacity`](https://tenacity.readthedocs.io/) | GitHub API çağrıları için üstel geri çekilmeli yeniden deneme |
| `asyncio` | SteamCMD için non-blocking subprocess; async indirme; RCON TCP istemcisi |
| `zipfile` | ZIP inceleme, çıkarma ve snapshot arşivleme (stdlib) |
| `struct` | Source RCON binary paket kodlama/çözme (stdlib) |

---

## Idempotency ve Durum

Her adım bir **dosya sistemi predikatıyla** korunur — diskteki gerçek dosyalar her zaman tek otorite.

```python
# gameinfo.gi asla iki kez yamalanmaz
def is_gameinfo_patched(csgo_dir: Path) -> bool:
    content = (csgo_dir / "gameinfo.gi").read_text(encoding="utf-8")
    return "csgo/addons/metamod" in content
```

Durumu iki eşlik dosyası takip eder:

| Dosya | Amaç |
|---|---|
| `install_state.json` | Faz başına tamamlanma zaman damgası ve sürüm bilgisi |
| `cs2-plugins.lock` | Eklenti başına tam sürüm, commit ref ve indirme URL'si |

Her ikisi de `os.replace()` ile atomik olarak yazılır. Birini silerek tam yeniden kontrol zorlanabilir — hiçbir şey bozulmaz.

---

## Lisans

MIT — [LICENSE](LICENSE) dosyasına bakın.
