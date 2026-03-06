#!/usr/bin/env python3
"""Minecraft Server Scanner - Async, Windows + Discord + Hardware Detection"""

import asyncio, socket, random, struct, json, time, argparse, sys, re, signal
import urllib.request, os, psutil, threading
from datetime import datetime, timezone

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1478636940541562921/AKULTFc0jYTH0JiW1_r_eDSGTedXYymC2L-LZgKpvFIgWtGn1LCZOj65y0fW4FM2bC7_"

# ─────────────────────────────────────────────────────
# Couleurs ANSI
# ─────────────────────────────────────────────────────
R  = "\033[0m"
G  = "\033[92m"    # vert  → serveur MC
P  = "\033[95m"    # violet → port ouvert sans MC
RE = "\033[91m"    # rouge → IP inaccessible
Y  = "\033[93m"    # jaune → stats
C  = "\033[96m"    # cyan
B  = "\033[1m"
M  = "\033[95m"

# ─────────────────────────────────────────────────────
# Détection hardware (90%)
# ─────────────────────────────────────────────────────
def detect_hardware():
    cpu_cores    = os.cpu_count() or 4
    vm           = psutil.virtual_memory()
    ram_total_gb = vm.total     / (1024**3)
    ram_avail_gb = vm.available / (1024**3)
    use_cores    = max(1, int(cpu_cores * 0.9))
    # Concurrence basée sur CPU uniquement (coroutines asyncio = très léger en RAM)
    # 300 connexions par core est raisonnable pour asyncio réseau
    concurrency  = max(500, min(use_cores * 300, 5000))
    return {
        "cpu_cores":    cpu_cores,
        "use_cores":    use_cores,
        "ram_total_gb": round(ram_total_gb, 1),
        "ram_avail_gb": round(ram_avail_gb, 1),
        "concurrency":  concurrency,
    }

# ─────────────────────────────────────────────────────
# Discord Webhook
# ─────────────────────────────────────────────────────
def _safe_str(v, fallback="?"):
    """Convertit n'importe quelle valeur en string non-vide pour Discord."""
    if v is None or v == "" or v == []: return fallback
    return str(v)[:1024]

def _webhook_post(payload_bytes):
    """POST synchrone vers Discord — appelé dans un thread daemon."""
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
        return f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return str(e)

def send_discord(ip, port, info: dict):
    """Lance l'envoi Discord dans un thread daemon (non-bloquant)."""
    def _run():
        try:
            names   = ", ".join(info.get("players_list",[])[:10]) or "Aucun"
            plugins = ", ".join(info.get("plugins",[])[:10]) or "—"
            mods    = ", ".join(info.get("mods",[])[:10])   or "—"
            wl      = info.get("whitelist")
            wl_str  = "✅ Activée" if wl is True else "❌ Désactivée" if wl is False else "❓ Inconnue"
            om      = info.get("online_mode")
            om_str  = "Online (premium)" if om is True else "Offline (crackée)" if om is False else "Inconnu"
            embed = {
                "title": "🟢 Serveur Minecraft trouvé !",
                "color": 0x00FF7F,
                "fields": [
                    {"name": "IP",        "value": f"`{ip}:{port}`",                                                         "inline": True},
                    {"name": "Version",   "value": _safe_str(info.get("version")),                                             "inline": True},
                    {"name": "Logiciel",  "value": _safe_str(info.get("software")),                                            "inline": True},
                    {"name": "Joueurs",   "value": f"{info.get('players_online',0)}/{info.get('players_max',0)}",              "inline": True},
                    {"name": "Auth",      "value": om_str,                                                                     "inline": True},
                    {"name": "Whitelist", "value": wl_str,                                                                     "inline": True},
                    {"name": "MOTD",      "value": _safe_str(info.get("motd"), "—"),                                           "inline": False},
                    {"name": "En ligne",  "value": _safe_str(names, "Aucun"),                                                  "inline": False},
                    {"name": "Mode jeu",  "value": _safe_str(info.get("gamemode")),                                            "inline": True},
                    {"name": "Difficulte","value": _safe_str(info.get("difficulty")),                                          "inline": True},
                    {"name": "Monde",     "value": _safe_str(info.get("level_name")),                                          "inline": True},
                    {"name": "Plugins",   "value": _safe_str(plugins, "—"),                                                    "inline": False},
                    {"name": "Mods",      "value": _safe_str(mods, "—"),                                                      "inline": False},
                ],
                "footer": {"text": "Minecraft Scanner"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
            result  = _webhook_post(payload)
            if isinstance(result, int):
                print(f"\n\033[92m[WEBHOOK] ✓ Envoyé {ip}:{port} (HTTP {result})\033[0m")
            else:
                print(f"\n\033[91m[WEBHOOK] ✗ Erreur {ip}:{port} → {result}\033[0m")
        except Exception as e:
            print(f"\n\033[91m[WEBHOOK] ✗ Exception {ip}:{port} → {e}\033[0m")

    threading.Thread(target=_run, daemon=True).start()

def send_discord_player_join(ip, port, player, online, max_players):
    """Notification de connexion d'un joueur."""
    def _run():
        try:
            embed = {
                "title": f"✅ {player} a rejoint le serveur",
                "color": 0x2ECC71,
                "fields": [
                    {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
                    {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
                ],
                "footer": {"text": "Minecraft Scanner - Connexion"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
            result = _webhook_post(payload)
            if isinstance(result, int):
                print(f"\n\033[92m[WEBHOOK] ✓ Connexion {player} envoyée (HTTP {result})\033[0m")
            else:
                print(f"\n\033[91m[WEBHOOK] ✗ Erreur connexion {player} → {result}\033[0m")
        except Exception as e:
            print(f"\n\033[91m[WEBHOOK] ✗ Exception join {player} → {e}\033[0m")
    threading.Thread(target=_run, daemon=True).start()


def send_discord_player_leave(ip, port, player, online, max_players):
    """Notification de déconnexion d'un joueur."""
    def _run():
        try:
            embed = {
                "title": f"🚪 {player} a quitté le serveur",
                "color": 0xE74C3C,
                "fields": [
                    {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
                    {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
                ],
                "footer": {"text": "Minecraft Scanner - Déconnexion"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
            result = _webhook_post(payload)
            if isinstance(result, int):
                print(f"\n\033[92m[WEBHOOK] ✓ Déconnexion {player} envoyée (HTTP {result})\033[0m")
            else:
                print(f"\n\033[91m[WEBHOOK] ✗ Erreur déconnexion {player} → {result}\033[0m")
        except Exception as e:
            print(f"\n\033[91m[WEBHOOK] ✗ Exception leave {player} → {e}\033[0m")
    threading.Thread(target=_run, daemon=True).start()


def send_discord_server_offline(ip, port):
    """Notification serveur inaccessible."""
    def _run():
        try:
            embed = {
                "title": "🔴 Serveur inaccessible",
                "color": 0x95A5A6,
                "fields": [
                    {"name": "🌐 Serveur", "value": f"`{ip}:{port}`", "inline": True},
                ],
                "footer": {"text": "Minecraft Scanner - Hors ligne"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
            _webhook_post(payload)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def send_discord_server_back_online(ip, port, online, max_players):
    """Notification serveur de retour en ligne."""
    def _run():
        try:
            embed = {
                "title": "🟢 Serveur de retour en ligne",
                "color": 0x00FF7F,
                "fields": [
                    {"name": "🌐 Serveur", "value": f"`{ip}:{port}`",          "inline": True},
                    {"name": "👥 Joueurs", "value": f"{online}/{max_players}", "inline": True},
                ],
                "footer": {"text": "Minecraft Scanner - En ligne"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
            _webhook_post(payload)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

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
RESERVED_INT = [(struct.unpack("!I",socket.inet_aton(s))[0],
                 struct.unpack("!I",socket.inet_aton(e))[0]) for s,e in RESERVED_RANGES]

def is_public_ip(ip):
    try:
        v = struct.unpack("!I", socket.inet_aton(ip))[0]
        return not any(s <= v <= e for s,e in RESERVED_INT)
    except: return False

def random_public_ip():
    while True:
        ip = f"{random.randint(1,254)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        if is_public_ip(ip): return ip

# ─────────────────────────────────────────────────────
# Minecraft protocol helpers
# ─────────────────────────────────────────────────────
def _write_varint(v):
    out = b""
    while True:
        b = v & 0x7F; v >>= 7
        out += bytes([b|0x80]) if v else bytes([b])
        if not v: break
    return out

async def _read_varint(reader):
    result, shift = 0, 0
    while True:
        b = (await reader.readexactly(1))[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80): return result
        shift += 7
        if shift >= 35: raise ValueError("VarInt overflow")

def _mc_handshake(ip, port):
    data = (b"\x00" + _write_varint(760) + _write_varint(len(ip))
            + ip.encode() + struct.pack(">H", port) + _write_varint(1))
    return _write_varint(len(data)) + data

def clean_motd(desc):
    if isinstance(desc, dict):
        # Gestion des extras (JSON text component)
        text = desc.get("text","")
        for extra in desc.get("extra",[]):
            if isinstance(extra, dict): text += extra.get("text","")
            else: text += str(extra)
    else:
        text = str(desc)
    return re.sub(r"[§&][0-9a-fk-orA-FK-OR]","",text).strip()

# ─────────────────────────────────────────────────────
# Extraction d'infos complètes depuis le status JSON
# ─────────────────────────────────────────────────────
def extract_info(ip, port, status: dict) -> dict:
    pl      = status.get("players", {})
    ver     = status.get("version", {})
    motd    = clean_motd(status.get("description", ""))
    sample  = pl.get("sample", [])

    # Détection logiciel / plugins / mods depuis version name
    ver_name = ver.get("name", "?")
    software = "Vanilla"
    if "Paper"   in ver_name: software = "Paper"
    elif "Spigot" in ver_name: software = "Spigot"
    elif "Bukkit" in ver_name: software = "Bukkit"
    elif "Forge"  in ver_name: software = "Forge"
    elif "Fabric" in ver_name: software = "Fabric"
    elif "BungeeCord" in ver_name: software = "BungeeCord"
    elif "Velocity"   in ver_name: software = "Velocity"
    elif "Waterfall"  in ver_name: software = "Waterfall"

    # Plugins (certains serveurs les exposent dans le status)
    plugins = []
    if "forgeData" in status:
        mods = [m.get("modId","?") for m in status["forgeData"].get("mods",[])]
    else:
        mods = []

    # Infos supplémentaires potentiellement exposées
    gamemode    = status.get("gamemode", status.get("game_mode", "?"))
    difficulty  = status.get("difficulty", "?")
    online_mode = status.get("online_mode", None)  # pas toujours présent
    level_name  = status.get("level_name", status.get("world_name", "?"))
    max_players = pl.get("max", 0)
    online      = pl.get("online", 0)
    favicon     = "Oui" if status.get("favicon") else "Non"

    # Détection whitelist :
    # Certains serveurs exposent explicitement "whitelist" dans le status.
    # Sinon, si players_max > 0 mais players_online == 0 et la liste sample est vide
    # ET que le serveur répond "You are not whitelisted" dans la description → whitelist ON.
    whitelist = None
    if "whitelist" in status:
        whitelist = bool(status["whitelist"])
    elif "white-list" in status:
        whitelist = bool(status["white-list"])
    else:
        # Certains serveurs mettent un message dans la description quand whitelisté
        motd_lower = motd.lower()
        if any(kw in motd_lower for kw in ["whitelist","white-list","white list","not whitelisted","liste blanche"]):
            whitelist = True
        # Si max > 0 mais slots affichés comme 0/0 c'est souvent whitelist aussi
        elif max_players == 0 and online == 0 and not sample:
            whitelist = None  # inconnu
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
        "players_list":   [p.get("name","?") for p in sample],
        "software":       software,
        "plugins":        plugins,
        "mods":           mods,
        "gamemode":       gamemode,
        "difficulty":     difficulty,
        "online_mode":    online_mode,
        "level_name":     level_name,
        "favicon":        favicon,
        "whitelist":      whitelist,
        "found_at":       datetime.now().isoformat(),
    }

# ─────────────────────────────────────────────────────
# Connexion + status MC
# ─────────────────────────────────────────────────────
async def mc_ping(ip, port, timeout) -> dict | None:
    """Retourne le dict status JSON ou None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
    except Exception:
        return None
    try:
        writer.write(_mc_handshake(ip, port))
        writer.write(b"\x01\x00")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        await asyncio.wait_for(_read_varint(reader), timeout=timeout)  # pkt len
        pkt_id = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if pkt_id != 0: return None
        jlen = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        if jlen > 65536: return None
        raw = await asyncio.wait_for(reader.readexactly(jlen), timeout=timeout)
        return json.loads(raw.decode("utf-8","replace"))
    except Exception:
        return None
    finally:
        try: writer.close(); await writer.wait_closed()
        except Exception: pass

# ─────────────────────────────────────────────────────
# Stats globales
# ─────────────────────────────────────────────────────
stats = {"scanned":0, "open":0, "found":0, "fail":0, "mc_timeout":0, "mc_fail":0, "start":time.monotonic()}
results_log: list[dict] = []   # liste des serveurs MC trouvés (mise à jour en live)
stop_flag = False

# ─────────────────────────────────────────────────────
# Affichage d'un résultat coloré
# ─────────────────────────────────────────────────────
def print_scan_result(ip, port, state, info=None):
    """state: 'fail' | 'open' | 'mc'"""
    if state == "fail":
        # Rouge - IP inaccessible / port fermé
        print(f"  {RE}✗ {ip}:{port}  →  Port fermé / Hôte inaccessible{R}")
    elif state == "open":
        # Violet - Port ouvert mais pas Minecraft
        print(f"  {P}~ {ip}:{port}  →  Port ouvert (pas Minecraft){R}")
    elif state == "mc" and info:
        # Vert - Serveur Minecraft !
        mods_str    = f"  {C}Mods    :{R} {', '.join(info['mods'][:8])}\n" if info["mods"] else ""
        plugins_str = f"  {C}Plugins :{R} {', '.join(info['plugins'][:8])}\n" if info["plugins"] else ""
        players_str = ""
        if info["players_list"]:
            players_str = f"  {C}En ligne:{R} {', '.join(info['players_list'][:10])}\n"
        print(
            f"\n{B}{G}╔══════════════════════════════════════════╗\n"
            f"║       SERVEUR MINECRAFT TROUVÉ !         ║\n"
            f"╚══════════════════════════════════════════╝{R}\n"
            f"  {C}IP       :{R} {B}{ip}:{port}{R}\n"
            f"  {C}Logiciel :{R} {info['software']}\n"
            f"  {C}Version  :{R} {info['version']}  (protocol {info['protocol']})\n"
            f"  {C}MOTD     :{R} {info['motd']}\n"
            f"  {C}Joueurs  :{R} {G}{B}{info['players_online']}{R}/{info['players_max']}\n"
            f"{players_str}"
            f"  {C}Mode jeu :{R} {info['gamemode']}\n"
            f"  {C}Auth     :{R} {'Online (premium)' if info['online_mode'] else 'Offline (crackée)' if info['online_mode'] is False else 'Inconnu'}\n"
            f"  {C}Whitelist:{R} {'\033[91mActivée\033[0m' if info['whitelist'] is True else '\033[92mDésactivée\033[0m' if info['whitelist'] is False else '\033[93mInconnue\033[0m'}\n"
            f"  {C}Difficulté:{R} {info['difficulty']}\n"
            f"  {C}Monde    :{R} {info['level_name']}\n"
            f"  {C}Favicon  :{R} {info['favicon']}\n"
            f"{mods_str}{plugins_str}"
        )

# ─────────────────────────────────────────────────────
# Scan d'une IP
# ─────────────────────────────────────────────────────
async def scan_ip(ip, port, timeout):
    # ── Étape 1 : connexion TCP ──────────────────────────
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
    except Exception:
        stats["scanned"] += 1
        stats["fail"] += 1
        print(f"\r  {RE}✗ {ip:<21}  Fermé / Hôte inaccessible{R}", end="", flush=True)
        return

    # ── Étape 2 : ping MC sur la même connexion ──────────
    stats["open"] += 1
    status = None
    fail_reason = ""
    try:
        # On envoie handshake + status request en un seul write pour éviter
        # que certains serveurs ignorent les paquets fragmentés
        writer.write(_mc_handshake(ip, port) + b"\x01\x00")
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        # Lecture réponse MC
        _pkt_len = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
        pkt_id   = await asyncio.wait_for(_read_varint(reader), timeout=timeout)
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
        # Violet : ligne qui s'écrase avec \r → visible sans flood
        print(f"\r  {P}~ {ip}:{port:<21}  Ouvert sans MC  [{fail_reason:<25}]{R}", end="", flush=True)
        return

    # ── Étape 3 : serveur MC confirmé ───────────────────
    stats["found"] += 1
    info = extract_info(ip, port, status)
    results_log.append(info)
    print_scan_result(ip, port, "mc", info)  # toujours affiché en vert
    send_discord(ip, port, info)

# ─────────────────────────────────────────────────────
# Pool de scan
# ─────────────────────────────────────────────────────
async def scanner_pool(port, timeout, concurrency):
    global stop_flag
    sem = asyncio.Semaphore(concurrency)
    tasks = set()
    async def bounded(ip):
        async with sem: await scan_ip(ip, port, timeout)
    while not stop_flag:
        ip = random_public_ip()
        task = asyncio.create_task(bounded(ip))
        tasks.add(task); task.add_done_callback(tasks.discard)
        while len(tasks) >= concurrency * 2 and not stop_flag:
            await asyncio.sleep(0.001)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

# ─────────────────────────────────────────────────────
# Mise à jour live des serveurs trouvés (toutes les secondes)
# ─────────────────────────────────────────────────────
async def refresh_servers(timeout):
    """Re-ping chaque serveur MC trouvé toutes les secondes pour màj les joueurs."""
    while not stop_flag:
        await asyncio.sleep(1)
        if not results_log:
            continue
        for entry in list(results_log):
            if stop_flag:
                break
            ip   = entry["ip"]
            port = entry["port"]
            was_offline = entry.get("status") == "offline"
            status = await mc_ping(ip, port, timeout)

            if status is None:
                # Serveur devenu inaccessible
                if entry.get("status") != "offline":
                    print(f"\n{RE}[OFFLINE]{R} {B}{ip}:{port}{R} → Serveur inaccessible")
                    send_discord_server_offline(ip, port)
                entry["players_online"] = 0
                entry["players_list"]   = []
                entry["status"]         = "offline"
                continue

            pl          = status.get("players", {})
            new_online  = pl.get("online", 0)
            new_max     = pl.get("max", entry["players_max"])
            new_list    = [p.get("name", "?") for p in pl.get("sample", [])]

            old_set = set(entry.get("players_list", []))
            new_set = set(new_list)

            joined = new_set - old_set   # joueurs qui viennent de se connecter
            left   = old_set - new_set   # joueurs qui viennent de se déconnecter

            entry["players_online"] = new_online
            entry["players_max"]    = new_max
            entry["players_list"]   = new_list
            entry["last_seen"]      = datetime.now().isoformat()
            entry["status"]         = "online"

            # Serveur de retour en ligne
            if was_offline:
                print(f"\n{G}[ONLINE]{R} {B}{ip}:{port}{R} → Serveur de retour en ligne "
                      f"({new_online}/{new_max})")
                send_discord_server_back_online(ip, port, new_online, new_max)

            # Joueurs qui ont rejoint
            for player in joined:
                print(f"\n{G}[+]{R} {B}{player}{R} a rejoint {B}{ip}:{port}{R} "
                      f"({new_online}/{new_max})")
                send_discord_player_join(ip, port, player, new_online, new_max)

            # Joueurs qui ont quitté
            for player in left:
                print(f"\n{RE}[-]{R} {B}{player}{R} a quitté {B}{ip}:{port}{R} "
                      f"({new_online}/{new_max})")
                send_discord_player_leave(ip, port, player, new_online, new_max)

# ─────────────────────────────────────────────────────
# Stats périodiques
# ─────────────────────────────────────────────────────
async def status_loop(interval):
    prev = 0
    while not stop_flag:
        await asyncio.sleep(interval)
        if stop_flag: break
        elapsed = time.monotonic() - stats["start"]
        rate    = (stats["scanned"] - prev) / interval
        prev    = stats["scanned"]
        avg     = stats["scanned"] / elapsed if elapsed > 0 else 0
        cpu_pct = psutil.cpu_percent()
        ram_pct = psutil.virtual_memory().percent

        # Résumé des serveurs actifs
        online_servers = [e for e in results_log if e.get("status","online") == "online"]
        total_players  = sum(e["players_online"] for e in online_servers)

        print(f"\n{Y}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}")
        print(f"{Y}[STATS]{R} Scannées: {B}{stats['scanned']}{R} | "
              f"{RE}Fermées:{R} {stats['fail']} | "
              f"{P}Sans MC:{R} {stats['mc_fail']} | "
              f"{G}Serveurs MC: {B}{stats['found']}{R} | "
              f"Vitesse: {B}{rate:.0f}{R} IP/s")
        print(f"        CPU: {M}{cpu_pct:.0f}%{R} | "
              f"RAM: {M}{ram_pct:.0f}%{R} | "
              f"Timeouts MC: {Y}{stats['mc_timeout']}{R} | "
              f"Joueurs en ligne: {G}{B}{total_players}{R}")
        if results_log:
            print(f"{Y}[SERVEURS ACTIFS]{R}")
            for e in results_log:
                st = f"{G}●{R}" if e.get("status","online")=="online" else f"{RE}●{R}"
                print(f"  {st} {B}{e['ip']}:{e['port']}{R}  "
                      f"{e['version']}  "
                      f"Joueurs: {G}{e['players_online']}{R}/{e['players_max']}  "
                      f"MOTD: {e['motd'][:40]}")
        print(f"{Y}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{R}\n")

# ─────────────────────────────────────────────────────
# Sauvegarde JSON
# ─────────────────────────────────────────────────────
def save_json(path):
    data = {
        "total_scanned":   stats["scanned"],
        "total_found":     stats["found"],
        "scan_duration_s": round(time.monotonic()-stats["start"], 1),
        "servers":         results_log
    }
    with open(path,"w",encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\n{G}Sauvegardes dans : {path}  ({len(results_log)} serveur(s)){R}")

# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
async def async_main(args):
    global stop_flag
    hw = detect_hardware()
    concurrency = args.concurrency if args.concurrency_forced else hw["concurrency"]

    print(f"""
{B}{G}╔══════════════════════════════════════════╗
║   Minecraft Random IP Scanner [ASYNC]    ║
╚══════════════════════════════════════════╝{R}

{B}{M}  [ Détection hardware - 90% des ressources ]{R}
  CPU   : {hw['cpu_cores']} cores  → utilise {hw['use_cores']} (90%)
  RAM   : {hw['ram_total_gb']} GB total  → {hw['ram_avail_gb']} GB dispo
  Conc. : {B}{concurrency}{R} connexions simultanées (auto-calculé depuis CPU)

  Légende : {RE}✗ Fermé/injoignable{R}  {P}~ Ouvert sans MC{R}  {G}✓ Serveur Minecraft{R}
  Discord  : {G}Webhook activée{R}
  Refresh  : Serveurs MC re-pingés toutes les secondes

{Y}Démarrage... (Ctrl+C pour arrêter proprement){R}
""")

    try:
        loop = asyncio.get_event_loop()
        try: loop.add_signal_handler(signal.SIGINT, lambda: globals().update(stop_flag=True))
        except NotImplementedError: pass
    except Exception: pass

    try:
        await asyncio.gather(
            scanner_pool(args.port, args.timeout, concurrency),
            status_loop(args.stats_interval),
            refresh_servers(args.timeout),
            return_exceptions=True)
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
  Temps total        : {elapsed:.1f}s
  Vitesse moyenne    : {stats['scanned']/elapsed:.0f} IP/s
""")
    save_json(args.output)

def main():
    global stop_flag
    parser = argparse.ArgumentParser(description="Minecraft Async Scanner")
    parser.add_argument("-n","--count",       type=int,   default=0)
    parser.add_argument("-c","--concurrency", type=int,   default=0,
                        help="Forcer la concurrence (défaut: auto 90%% hardware)")
    parser.add_argument("-p","--port",        type=int,   default=25565)
    parser.add_argument("--timeout",          type=float, default=1.0)
    parser.add_argument("--output",           type=str,   default="minecraft_found.json")
    parser.add_argument("--stats-interval",   type=float, default=10.0)
    args = parser.parse_args()
    args.concurrency_forced = (args.concurrency > 0)
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
