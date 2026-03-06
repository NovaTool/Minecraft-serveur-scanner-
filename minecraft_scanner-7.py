#!/usr/bin/env python3
"""Minecraft Server Scanner - Async, Termux/Pixel 9 optimized + Discord player tracking"""

import asyncio, socket, random, struct, json, time, argparse, sys, re, signal
import urllib.request, os, psutil
from datetime import datetime, timezone

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1478636940541562921/AKULTFc0jYTH0JiW1_r_eDSGTedXYymC2L-LZgKpvFIgWtGn1LCZOj65y0fW4FM2bC7_"

# ─────────────────────────────────────────────────────
# Couleurs ANSI
# ─────────────────────────────────────────────────────
R  = "\033[0m"
G  = "\033[92m"
P  = "\033[95m"
RE = "\033[91m"
Y  = "\033[93m"
C  = "\033[96m"
B  = "\033[1m"
M  = "\033[95m"

# ─────────────────────────────────────────────────────
# Augmenter la limite fd Android (Termux)
# ─────────────────────────────────────────────────────
def _raise_fd_limit():
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(hard, 8192)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            return target
        return soft
    except Exception:
        return 1024

FD_LIMIT = _raise_fd_limit()

# ─────────────────────────────────────────────────────
# Détection hardware (80% — mobile-safe)
# ─────────────────────────────────────────────────────
def detect_hardware():
    cpu_cores    = os.cpu_count() or 4
    vm           = psutil.virtual_memory()
    ram_total_gb = vm.total     / (1024**3)
    ram_avail_gb = vm.available / (1024**3)
    use_cores    = max(1, int(cpu_cores * 0.8))
    # Sur Android/Termux, limité par les fd disponibles (laisse 512 de marge)
    max_by_fd    = max(200, FD_LIMIT - 512)
    concurrency  = max(300, min(use_cores * 250, 3000, max_by_fd))
    return {
        "cpu_cores":    cpu_cores,
        "use_cores":    use_cores,
        "ram_total_gb": round(ram_total_gb, 1),
        "ram_avail_gb": round(ram_avail_gb, 1),
        "concurrency":  concurrency,
        "fd_limit":     FD_LIMIT,
    }

# ─────────────────────────────────────────────────────
# Stats globales
# ─────────────────────────────────────────────────────
stats = {
    "scanned": 0, "open": 0, "found": 0, "fail": 0,
    "mc_timeout": 0, "mc_fail": 0, "start": time.monotonic(),
    "webhook_sent": 0, "webhook_err": 0, "webhook_drop": 0,
}
results_log: list[dict] = []
stop_flag = False
verbose = False   # activé avec --verbose
MAX_RESULTS = 300          # cap mémoire
CPU_THROTTLE_HIGH = 85.0   # % CPU → on ralentit
CPU_THROTTLE_LOW  = 70.0   # % CPU → on relâche

# ─────────────────────────────────────────────────────
# Queue webhook async — un seul worker, rate-limit Discord
# ─────────────────────────────────────────────────────
_webhook_queue: asyncio.Queue = None   # initialisée dans async_main

def _safe_str(v, fallback="?"):
    if v is None or v == "" or v == []: return fallback
    return str(v)[:1024]

def _webhook_post_sync(payload_bytes: bytes) -> int | str:
    """POST bloquant vers Discord (lancé via run_in_executor)."""
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com, 1.0)"
            }, method="POST")
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        return f"HTTP {e.code}: {body}"
    except Exception as e:
        return str(e)

async def webhook_worker():
    """Worker unique — consomme la queue et respecte le rate-limit Discord."""
    RATE_INTERVAL = 0.22   # ~4.5 req/s, sous la limite Discord de 5/s
    last_send = 0.0
    loop = asyncio.get_event_loop()

    while not stop_flag:
        try:
            payload_bytes = await asyncio.wait_for(_webhook_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        # Respecter le rate-limit
        wait = RATE_INTERVAL - (time.monotonic() - last_send)
        if wait > 0:
            await asyncio.sleep(wait)

        result = await loop.run_in_executor(None, _webhook_post_sync, payload_bytes)
        last_send = time.monotonic()
        _webhook_queue.task_done()

        if isinstance(result, int) and result in (200, 204):
            stats["webhook_sent"] += 1
        else:
            stats["webhook_err"] += 1
            if isinstance(result, str) and "429" in result:
                # Rate-limited par Discord → pause 5 s
                await asyncio.sleep(5.0)

def _enqueue(embed: dict):
    """Ajoute un embed à la queue (non-bloquant, thread-safe)."""
    if _webhook_queue is None:
        return
    payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
    try:
        _webhook_queue.put_nowait(payload)
    except asyncio.QueueFull:
        stats["webhook_drop"] += 1

# ─────────────────────────────────────────────────────
# Embeds Discord
# ─────────────────────────────────────────────────────
def send_discord(ip, port, info: dict):
    names   = ", ".join(info.get("players_list", [])[:10]) or "Aucun"
    plugins = ", ".join(info.get("plugins", [])[:10]) or "—"
    mods    = ", ".join(info.get("mods", [])[:10])    or "—"
    wl      = info.get("whitelist")
    wl_str  = "✅ Activée" if wl is True else "❌ Désactivée" if wl is False else "❓ Inconnue"
    om      = info.get("online_mode")
    om_str  = "Online (premium)" if om is True else "Offline (crackée)" if om is False else "Inconnu"
    _enqueue({
        "title": "🟢 Serveur Minecraft trouvé !",
        "color": 0x00FF7F,
        "fields": [
            {"name": "IP",         "value": f"`{ip}:{port}`",                                          "inline": True},
            {"name": "Version",    "value": _safe_str(info.get("version")),                            "inline": True},
            {"name": "Logiciel",   "value": _safe_str(info.get("software")),                           "inline": True},
            {"name": "Joueurs",    "value": f"{info.get('players_online',0)}/{info.get('players_max',0)}", "inline": True},
            {"name": "Auth",       "value": om_str,                                                    "inline": True},
            {"name": "Whitelist",  "value": wl_str,                                                    "inline": True},
            {"name": "MOTD",       "value": _safe_str(info.get("motd"), "—"),                          "inline": False},
            {"name": "En ligne",   "value": _safe_str(names, "Aucun"),                                 "inline": False},
            {"name": "Mode jeu",   "value": _safe_str(info.get("gamemode")),                           "inline": True},
            {"name": "Difficulté", "value": _safe_str(info.get("difficulty")),                         "inline": True},
            {"name": "Monde",      "value": _safe_str(info.get("level_name")),                         "inline": True},
            {"name": "Plugins",    "value": _safe_str(plugins, "—"),                                   "inline": False},
            {"name": "Mods",       "value": _safe_str(mods, "—"),                                      "inline": False},
        ],
        "footer": {"text": "Minecraft Scanner"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

def send_discord_player_join(ip, port, player, online, max_players):
    _enqueue({
        "title": f"✅ {player} a rejoint le serveur",
        "color": 0x2ECC71,
        "fields": [
            {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
            {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
        ],
        "footer": {"text": "Minecraft Scanner - Connexion"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

def send_discord_player_leave(ip, port, player, online, max_players):
    _enqueue({
        "title": f"🚪 {player} a quitté le serveur",
        "color": 0xE74C3C,
        "fields": [
            {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
            {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
        ],
        "footer": {"text": "Minecraft Scanner - Déconnexion"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

def send_discord_server_offline(ip, port):
    _enqueue({
        "title": "🔴 Serveur inaccessible",
        "color": 0x95A5A6,
        "fields": [{"name": "🌐 Serveur", "value": f"`{ip}:{port}`", "inline": True}],
        "footer": {"text": "Minecraft Scanner - Hors ligne"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

def send_discord_server_back_online(ip, port, online, max_players):
    _enqueue({
        "title": "🟢 Serveur de retour en ligne",
        "color": 0x00FF7F,
        "fields": [
            {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
            {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
        ],
        "footer": {"text": "Minecraft Scanner - En ligne"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

# ─────────────────────────────────────────────────────
# IPs réservées
# ─────────────────────────────────────────────────────
RESERVED_RANGES = [
    ("0.0.0.0","0.255.255.255"),("10.0.0.0","10.255.255.255"),
    ("100.64.0.0","100.127.255.255"),("127.0.0.0","127.255.255.255"),
    ("169.254.0.0","169.254.255.255"),("172.16.0.0","172.31.255.255"),
    ("192.0.0.0","192.0.0.255"),("192.168.0.0","192.168.255.255"),
    ("198.18.0.0","198.19.255.255"),("198.51.100.0","198.51.100.255"),
    ("203.0.113.0","203.0.113.255"),("224.0.0.0","255.255.255.255"),
]
RESERVED_INT = [(struct.unpack("!I", socket.inet_aton(s))[0],
                 struct.unpack("!I", socket.inet_aton(e))[0]) for s, e in RESERVED_RANGES]

def is_public_ip(ip_int: int) -> bool:
    return not any(s <= ip_int <= e for s, e in RESERVED_INT)

def _ip_from_int(n: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", n))

def generate_ip_batch(size: int = 512) -> list[str]:
    """Génère un lot d'IPs publiques aléatoires d'un seul coup (plus rapide que 1 par 1)."""
    batch = []
    while len(batch) < size:
        # Générer plusieurs entiers 32-bit d'un coup
        candidates = [random.getrandbits(32) for _ in range(size * 2)]
        for n in candidates:
            if len(batch) >= size:
                break
            # Éviter x.0.0.x et x.255.255.x
            if (n & 0xFF) in (0, 255) or ((n >> 24) & 0xFF) in (0, 255):
                continue
            if is_public_ip(n):
                batch.append(_ip_from_int(n))
    return batch

# ─────────────────────────────────────────────────────
# Minecraft protocol helpers
# ─────────────────────────────────────────────────────
def _write_varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F; v >>= 7
        out += bytes([b | 0x80]) if v else bytes([b])
        if not v: break
    return out

async def _read_varint(reader) -> int:
    result, shift = 0, 0
    while True:
        b = (await reader.readexactly(1))[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80): return result
        shift += 7
        if shift >= 35: raise ValueError("VarInt overflow")

def _mc_handshake(ip: str, port: int) -> bytes:
    data = (b"\x00" + _write_varint(760) + _write_varint(len(ip))
            + ip.encode() + struct.pack(">H", port) + _write_varint(1))
    return _write_varint(len(data)) + data

# Paquet handshake + status request pré-calculé pour la partie fixe
_STATUS_REQUEST = b"\x01\x00"

def clean_motd(desc) -> str:
    if isinstance(desc, dict):
        text = desc.get("text", "")
        for extra in desc.get("extra", []):
            text += extra.get("text", "") if isinstance(extra, dict) else str(extra)
    else:
        text = str(desc)
    return re.sub(r"[§&][0-9a-fk-orA-FK-OR]", "", text).strip()

# ─────────────────────────────────────────────────────
# Extraction infos serveur
# ─────────────────────────────────────────────────────
_SOFTWARE_KEYWORDS = [
    ("Paper", "Paper"), ("Spigot", "Spigot"), ("Bukkit", "Bukkit"),
    ("Forge", "Forge"), ("Fabric", "Fabric"), ("BungeeCord", "BungeeCord"),
    ("Velocity", "Velocity"), ("Waterfall", "Waterfall"),
]

def extract_info(ip: str, port: int, status: dict) -> dict:
    pl       = status.get("players", {})
    ver      = status.get("version", {})
    motd     = clean_motd(status.get("description", ""))
    sample   = pl.get("sample", [])
    ver_name = ver.get("name", "?")

    software = next((s for k, s in _SOFTWARE_KEYWORDS if k in ver_name), "Vanilla")
    mods = [m.get("modId", "?") for m in status.get("forgeData", {}).get("mods", [])]

    max_players = pl.get("max", 0)
    online      = pl.get("online", 0)
    motd_lower  = motd.lower()

    if "whitelist" in status:
        whitelist = bool(status["whitelist"])
    elif "white-list" in status:
        whitelist = bool(status["white-list"])
    elif any(kw in motd_lower for kw in ["whitelist", "white-list", "not whitelisted", "liste blanche"]):
        whitelist = True
    elif max_players == 0 and online == 0 and not sample:
        whitelist = None
    else:
        whitelist = False

    return {
        "ip":             ip,
        "port":           port,
        "version":        ver_name,
        "protocol":       ver.get("protocol", "?"),
        "motd":           motd,
        "players_online": online,
        "players_max":    max_players,
        "players_list":   [p.get("name", "?") for p in sample],
        "software":       software,
        "plugins":        [],
        "mods":           mods,
        "gamemode":       status.get("gamemode", status.get("game_mode", "?")),
        "difficulty":     status.get("difficulty", "?"),
        "online_mode":    status.get("online_mode", None),
        "level_name":     status.get("level_name", status.get("world_name", "?")),
        "favicon":        "Oui" if status.get("favicon") else "Non",
        "whitelist":      whitelist,
        "found_at":       datetime.now().isoformat(),
        "status":         "online",
        "offline_count":  0,
    }

# ─────────────────────────────────────────────────────
# Connexion + status MC
# ─────────────────────────────────────────────────────
async def mc_ping(ip: str, port: int, timeout: float) -> dict | None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
    except Exception:
        return None
    try:
        writer.write(_mc_handshake(ip, port) + _STATUS_REQUEST)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        await asyncio.wait_for(_read_varint(reader), timeout=timeout)   # pkt len
        pkt_id = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if pkt_id != 0: return None
        jlen = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if jlen > 65536: return None
        raw = await asyncio.wait_for(reader.readexactly(jlen), timeout=timeout)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    finally:
        try: writer.close(); await writer.wait_closed()
        except Exception: pass

# ─────────────────────────────────────────────────────
# Affichage : une ligne rouge fixe qui s'écrase, lignes importantes qui scrollent
# ─────────────────────────────────────────────────────
_IS_TTY = sys.stdout.isatty()

def _print_above(text: str):
    """Affiche une ligne permanente (violet/vert/stats) depuis la colonne 0."""
    sys.stdout.write(f"\r{text}\n")
    sys.stdout.flush()

def _print_status(text: str):
    """Met à jour la ligne rouge unique en bas — seulement si terminal interactif."""
    if not _IS_TTY:
        return  # Pas de \r dans les fichiers log (crée des lignes multiples)
    sys.stdout.write(f"\r{text:<60}")
    sys.stdout.flush()

# Raisons à ignorer pour le violet (faux positifs)
_SKIP_REASONS = ("timeout", "connection reset", "0 bytes", "eof", "reset by peer",
                 "broken pipe", "connection refused")

def print_scan_result(ip, port, info):
    wl = info["whitelist"]
    wl_color = RE if wl is True else G if wl is False else Y
    wl_txt   = "Oui" if wl is True else "Non" if wl is False else "?"
    auth     = "Premium" if info['online_mode'] else "Crackée" if info['online_mode'] is False else "?"
    players  = ", ".join(info['players_list'][:5]) or "—"
    mods_str = f"  {C}Mods  :{R} {', '.join(info['mods'][:5])}\n" if info["mods"] else ""
    motd     = info['motd'][:30] + ("…" if len(info['motd']) > 30 else "")
    _print_above(
        f"{B}{G}┌─[ SERVEUR MC TROUVÉ ]{'─'*14}┐{R}\n"
        f"  {C}IP     {R} {B}{ip}:{port}{R}\n"
        f"  {C}Version{R} {info['version']}\n"
        f"  {C}MOTD   {R} {motd}\n"
        f"  {C}Joueurs{R} {G}{B}{info['players_online']}{R}/{info['players_max']}  {players}\n"
        f"  {C}Auth   {R} {auth}  {C}WL:{R} {wl_color}{wl_txt}{R}  {C}SW:{R} {info['software']}\n"
        f"{mods_str}"
        f"{B}{G}└{'─'*35}┘{R}"
    )

# ─────────────────────────────────────────────────────
# Throttling adaptatif (CPU)
# ─────────────────────────────────────────────────────
_throttle_sleep = 0.0   # délai injecté dans le scanner_pool

async def cpu_throttle_loop():
    """Ajuste _throttle_sleep selon la charge CPU."""
    global _throttle_sleep
    while not stop_flag:
        await asyncio.sleep(3)
        cpu = psutil.cpu_percent(interval=None)
        if cpu > CPU_THROTTLE_HIGH:
            _throttle_sleep = min(_throttle_sleep + 0.002, 0.05)
        elif cpu < CPU_THROTTLE_LOW:
            _throttle_sleep = max(_throttle_sleep - 0.001, 0.0)

# ─────────────────────────────────────────────────────
# Scan d'une IP
# ─────────────────────────────────────────────────────
async def scan_ip(ip: str, port: int, timeout: float):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
    except Exception:
        stats["scanned"] += 1
        stats["fail"] += 1
        _print_status(f"{RE}✗{R} {ip:<21}")
        return

    stats["open"] += 1
    status = None
    fail_reason = ""
    try:
        writer.write(_mc_handshake(ip, port) + _STATUS_REQUEST)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        pkt_id = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if pkt_id != 0:
            fail_reason = f"bad_pkt_id={pkt_id}"
        else:
            jlen = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
            if jlen > 65536:
                fail_reason = f"json_too_large={jlen}"
            else:
                raw    = await asyncio.wait_for(reader.readexactly(jlen), timeout=timeout)
                status = json.loads(raw.decode("utf-8", "replace"))
    except asyncio.TimeoutError:
        stats["mc_timeout"] += 1
        fail_reason = "timeout"
    except Exception as exc:
        fail_reason = str(exc)[:40]
    finally:
        try: writer.close(); await writer.wait_closed()
        except: pass

    stats["scanned"] += 1

    if status is None:
        stats["mc_fail"] += 1
        if not any(r in fail_reason.lower() for r in _SKIP_REASONS):
            _print_above(f"{P}~ {ip}:{port}  {fail_reason[:40]}{R}")
        return

    stats["found"] += 1
    info = extract_info(ip, port, status)

    # Cap mémoire : supprimer les plus vieux si on dépasse MAX_RESULTS
    if len(results_log) >= MAX_RESULTS:
        results_log.pop(0)

    results_log.append(info)
    print_scan_result(ip, port, info)
    send_discord(ip, port, info)

# ─────────────────────────────────────────────────────
# Pool de scan avec génération en batch + throttle adaptatif
# ─────────────────────────────────────────────────────
async def scanner_pool(port: int, timeout: float, concurrency: int):
    global stop_flag
    sem   = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task] = set()
    ip_batch: list[str] = []

    async def bounded(ip):
        async with sem:
            await scan_ip(ip, port, timeout)

    while not stop_flag:
        # Régénérer un batch si vide
        if not ip_batch:
            ip_batch = generate_ip_batch(512)

        ip = ip_batch.pop()
        task = asyncio.create_task(bounded(ip))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        # Throttle adaptatif
        if _throttle_sleep > 0:
            await asyncio.sleep(_throttle_sleep)

        # Backpressure : attendre si trop de tâches en vol
        while len(tasks) >= concurrency * 2 and not stop_flag:
            await asyncio.sleep(0.005)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

# ─────────────────────────────────────────────────────
# Refresh serveurs trouvés — backoff exponentiel si offline
# ─────────────────────────────────────────────────────
async def refresh_servers(timeout: float):
    """Re-ping chaque serveur toutes les secondes, avec backoff si offline."""
    while not stop_flag:
        await asyncio.sleep(1)
        if not results_log:
            continue

        for entry in list(results_log):
            if stop_flag:
                break

            ip   = entry["ip"]
            port = entry["port"]

            # Backoff exponentiel pour les serveurs offline (max 64 s)
            offline_count = entry.get("offline_count", 0)
            if offline_count > 0:
                backoff = min(2 ** offline_count, 64)
                last_check = entry.get("_last_check", 0)
                if time.monotonic() - last_check < backoff:
                    continue

            entry["_last_check"] = time.monotonic()
            was_offline = entry.get("status") == "offline"
            status = await mc_ping(ip, port, timeout)

            if status is None:
                entry["players_online"] = 0
                entry["players_list"]   = []
                entry["status"]         = "offline"
                entry["offline_count"]  = offline_count + 1
                continue

            # Serveur répond
            entry["offline_count"] = 0
            pl         = status.get("players", {})
            new_online = pl.get("online", 0)
            new_max    = pl.get("max", entry["players_max"])
            new_list   = [p.get("name", "?") for p in pl.get("sample", [])]

            def _norm(name: str) -> str:
                """Supprime codes §X et timestamps HH:MM:SS pour éviter fausses alertes."""
                name = re.sub(r'§.', '', name)
                name = re.sub(r'\d{2}:\d{2}:\d{2}', '', name).strip()
                return name

            old_norm = {_norm(p): p for p in entry.get("players_list", [])}
            new_norm = {_norm(p): p for p in new_list}
            joined   = [new_norm[k] for k in set(new_norm) - set(old_norm)]
            left     = [old_norm[k] for k in set(old_norm) - set(new_norm)]

            entry["players_online"] = new_online
            entry["players_max"]    = new_max
            entry["players_list"]   = new_list
            entry["last_seen"]      = datetime.now().isoformat()
            entry["status"]         = "online"

            if was_offline:
                _print_above(f"{G}[ONLINE]{R} {B}{ip}:{port}{R} → Retour en ligne ({new_online}/{new_max})")
                if new_online > 0:
                    send_discord_server_back_online(ip, port, new_online, new_max)

            for player in joined:
                _print_above(f"{G}[+]{R} {B}{player}{R} a rejoint {B}{ip}:{port}{R} ({new_online}/{new_max})")
                send_discord_player_join(ip, port, player, new_online, new_max)

            for player in left:
                _print_above(f"{RE}[-]{R} {B}{player}{R} a quitté {B}{ip}:{port}{R} ({new_online}/{new_max})")
                send_discord_player_leave(ip, port, player, new_online, new_max)

# ─────────────────────────────────────────────────────
# Stats périodiques
# ─────────────────────────────────────────────────────
async def status_loop(interval: float):
    prev = 0
    while not stop_flag:
        await asyncio.sleep(interval)
        if stop_flag: break

        elapsed = time.monotonic() - stats["start"]
        rate    = (stats["scanned"] - prev) / interval
        prev    = stats["scanned"]
        cpu_pct = psutil.cpu_percent(interval=None)
        ram_pct = psutil.virtual_memory().percent

        online_servers = [e for e in results_log if e.get("status", "online") == "online"]
        total_players  = sum(e["players_online"] for e in online_servers)

        thr = f" {Y}~{_throttle_sleep*1000:.0f}ms{R}" if _throttle_sleep > 0 else ""

        rows = [f"{Y}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}"]
        rows.append(
            f"{Y}▸{R} {B}{rate:.0f}{R} IP/s  "
            f"{G}{stats['found']} MC{R}  "
            f"CPU {M}{cpu_pct:.0f}%{R}  "
            f"RAM {M}{ram_pct:.0f}%{R}{thr}"
        )
        rows.append(
            f"  {C}Scannées:{R} {stats['scanned']}  "
            f"{C}WH:{R} {G}{stats['webhook_sent']}{R}✓ {RE}{stats['webhook_err']}{R}✗"
        )
        for e in results_log:
            st = f"{G}●{R}" if e.get("status", "online") == "online" else f"{RE}●{R}"
            pl = f"{G}{e['players_online']}{R}/{e['players_max']}"
            rows.append(f"  {st} {B}{e['ip']}:{e['port']}{R} {pl}j  {e['motd'][:22]}")
        rows.append(f"{Y}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}")
        _print_above("\n".join(rows))

# ─────────────────────────────────────────────────────
# Sauvegarde JSON
# ─────────────────────────────────────────────────────
def save_json(path: str):
    data = {
        "total_scanned":   stats["scanned"],
        "total_found":     stats["found"],
        "scan_duration_s": round(time.monotonic() - stats["start"], 1),
        "servers":         results_log,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _print_above(f"{G}Sauvegarde : {path}  ({len(results_log)} serveur(s)){R}")

# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
async def async_main(args):
    global stop_flag, _webhook_queue

    _webhook_queue = asyncio.Queue(maxsize=500)

    hw = detect_hardware()
    concurrency = args.concurrency if args.concurrency_forced else hw["concurrency"]

    print(f"""
{B}{G}┌─[ MC Scanner — Termux ]──────────────┐{R}
  CPU  {hw['cpu_cores']}c → {hw['use_cores']}c (80%)  RAM {hw['ram_avail_gb']}GB dispo
  Conc {B}{concurrency}{R}  FD {hw['fd_limit']}  Throttle >{CPU_THROTTLE_HIGH:.0f}%
{B}{G}└───────────────────────────────────────┘{R}
{Y}Démarrage...  Ctrl+C pour arrêter{R}
""")

    # Gestion signal (Termux compatible)
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: globals().update(stop_flag=True))
            except (NotImplementedError, OSError):
                pass
    except Exception:
        pass

    try:
        await asyncio.gather(
            scanner_pool(args.port, args.timeout, concurrency),
            status_loop(args.stats_interval),
            refresh_servers(args.timeout),
            webhook_worker(),
            cpu_throttle_loop(),
            return_exceptions=True,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_flag = True

    elapsed = time.monotonic() - stats["start"]
    print(f"""
{B}╔══════════════════════════════════════════╗
║           RÉSULTATS FINAUX               ║
╚══════════════════════════════════════════╝{R}
  IPs scannées       : {stats['scanned']}
  Ports ouverts      : {stats['open']}
  Serveurs Minecraft : {G}{B}{stats['found']}{R}
  Webhooks envoyés   : {G}{stats['webhook_sent']}{R}
  Temps total        : {elapsed:.1f}s
  Vitesse moyenne    : {stats['scanned']/max(elapsed,1):.0f} IP/s
""")
    save_json(args.output)

def main():
    global stop_flag, verbose
    parser = argparse.ArgumentParser(description="Minecraft Async Scanner — Termux/Pixel 9")
    parser.add_argument("-c", "--concurrency", type=int,   default=0,
                        help="Forcer la concurrence (défaut: auto 80%% hardware)")
    parser.add_argument("-p", "--port",        type=int,   default=25565)
    parser.add_argument("--timeout",           type=float, default=1.5)
    parser.add_argument("--output",            type=str,   default="minecraft_found.json")
    parser.add_argument("--stats-interval",    type=float, default=10.0)
    parser.add_argument("-v", "--verbose",     action="store_true",
                        help="Afficher toutes les IPs (fermées/sans MC)")
    args = parser.parse_args()
    args.concurrency_forced = (args.concurrency > 0)
    verbose = args.verbose

    try:
        asyncio.run(async_main(args))
    except (KeyboardInterrupt, SystemExit):
        stop_flag = True
    finally:
        if results_log:
            try: save_json(args.output)
            except Exception: pass

if __name__ == "__main__":
    main()
