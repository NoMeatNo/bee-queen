import json
import hashlib
import requests
import re
import concurrent.futures
from urllib.parse import quote_plus
import urllib3
import sys

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Target channel
TARGET_CHANNELS = ["ITV 1", "ITV1"]

# Quality filtering
QUALITY_FILTER = "hevc"

# Handshake timeout and attempts config
HANDSHAKE_TIMEOUT = 3.0
FETCH_TIMEOUT = 5.0
MAX_MAC_ATTEMPTS = 5
USER_AGENT = "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"

def load_servers_config():
    with open("./matrix/plugin.video.hublive/servers.json", "r") as f:
        return json.load(f)

def build_device_identity(mac):
    mac_upper = (mac or "").strip().upper()
    serialnumber = hashlib.md5(mac_upper.encode()).hexdigest().upper()
    device_id2 = hashlib.sha256(mac_upper.encode()).hexdigest().upper()
    hw_version_2 = hashlib.sha1(mac_upper.encode()).hexdigest()
    return serialnumber, device_id2, hw_version_2

def build_auth_headers_and_cookies(portal_url, mac, token, random_value="0"):
    mac_upper = (mac or "").strip().upper()
    identity = build_device_identity(mac_upper)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "X-User-Agent": "Model: MAG250; Link:",
        "Referer": f"{portal_url}/c/",
        "Authorization": f"Bearer {token}" if token else "",
    }
    cookies = {
        "mac": mac_upper,
        "stb_lang": "en",
        "timezone": "Europe/Bucharest",
        "device_id": identity[0],
        "device_id2": identity[1],
        "hw_version_2": identity[2],
    }
    if random_value:
        cookies["random"] = random_value
    return headers, cookies

def handshake(portal_url, mac):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, "")
    try:
        url = f"{portal_url}/server/load.php?type=stb&action=handshake&token=&Vs=1&vc=1"
        response = requests.get(url, headers=headers, cookies=cookies, timeout=HANDSHAKE_TIMEOUT, verify=False)
        data = response.json()
        token = data.get("js", {}).get("token")
        random_value = data.get("js", {}).get("random", "0")
        if token:
            headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
            profile_url = f"{portal_url}/server/load.php?type=stb&action=get_profile"
            requests.get(profile_url, headers=headers, cookies=cookies, timeout=HANDSHAKE_TIMEOUT, verify=False)
            return token, random_value
    except Exception:
        pass
    return None, None

def fetch_channels(portal_url, mac, token, random_value):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
    url = f"{portal_url}/server/load.php?type=itv&action=get_all_channels"
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=FETCH_TIMEOUT, verify=False)
        data = response.json()
        if "js" in data and isinstance(data["js"], dict) and "data" in data["js"]:
            return data["js"]["data"]
    except Exception as e:
        print(f"Error fetching channels for {portal_url}: {e}")
    return []

def _extract_play_token(returned_cmd):
    play_token_match = re.search(r"play_token=([a-zA-Z0-9]+)", returned_cmd or "")
    if play_token_match:
        return play_token_match.group(1)
    return None

def get_channel_link(ch):
    headers, cookies = build_auth_headers_and_cookies(ch["portal"], ch["mac"], ch["token"], ch["random"])

    # Try the same strategy the Kodi addon uses
    create_link_url = f"{ch['portal']}/portal.php?type=itv&action=create_link&cmd={quote_plus(ch['cmd'])}&JsHttpRequest=1-xml"
    try:
        response = requests.get(create_link_url, headers=headers, cookies=cookies, timeout=FETCH_TIMEOUT, verify=False)
        data = response.json()
        if "js" in data and isinstance(data["js"], dict) and "cmd" in data["js"]:
            returned_cmd = data["js"]["cmd"]

            # If server returned ffmpeg standard stream line, return it
            if returned_cmd and returned_cmd.startswith("ffmpeg "):
                return ch, returned_cmd

            # Otherwise extract play_token to build the direct live.php link like Kodi does
            play_token = _extract_play_token(returned_cmd)
            if play_token:
                stream_id_match = re.search(r"(\d+)", ch["cmd"])
                stream_id = stream_id_match.group(1) if stream_id_match else ch["cmd"]
                final_url = f"{ch['portal'].rstrip('/')}/play/live.php?mac={ch['mac']}&stream={stream_id}&extension=ts&play_token={play_token}"
                return ch, final_url
    except Exception:
        pass
    return ch, None

def normalize_name(name):
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    return name.lower().strip()

def filter_by_quality(name, quality_filter):
    if not quality_filter:
        return True
    return quality_filter.lower() in name.lower()

def process_server(server):
    portal_url = server.get("portal_url")
    server_name = server.get("name", "Unknown Server")
    if not portal_url:
        return []

    token = None
    random_value = None
    working_mac = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        macs_to_test = server.get("macs", [])[:MAX_MAC_ATTEMPTS]
        future_to_mac = {executor.submit(handshake, portal_url, mac): mac for mac in macs_to_test}
        for future in concurrent.futures.as_completed(future_to_mac):
            mac = future_to_mac[future]
            try:
                t, r = future.result()
                if t:
                    token = t
                    random_value = r
                    working_mac = mac
                    break
            except Exception:
                pass

    if not token:
        return []

    channels = fetch_channels(portal_url, working_mac, token, random_value)

    server_channels = []
    for ch in channels:
        ch_name = ch.get("name", "")
        if not filter_by_quality(ch_name, QUALITY_FILTER):
            continue

        ch_norm = normalize_name(ch_name)
        for target in TARGET_CHANNELS:
            target_norm = normalize_name(target)
            if target_norm in ch_norm:
                cmd = ch.get("cmd")
                if cmd:
                    server_channels.append({
                        "name": ch_name,
                        "target": target,
                        "portal": portal_url,
                        "mac": working_mac,
                        "token": token,
                        "random": random_value,
                        "cmd": cmd,
                        "server_name": server_name
                    })
                break
    return server_channels

def main():
    config = load_servers_config()
    all_channels_found = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_server = {executor.submit(process_server, server): server for server in config.get("servers", [])}
        for future in concurrent.futures.as_completed(future_to_server):
            all_channels_found.extend(future.result())

    if not all_channels_found:
        sys.exit(0)

    resolved_channels = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_ch = {executor.submit(get_channel_link, ch): ch for ch in all_channels_found}
        for future in concurrent.futures.as_completed(future_to_ch):
            ch, link = future.result()
            if link:
                ch["resolved_link"] = link
                resolved_channels.append(ch)

    with open("hublive.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in resolved_channels:
            link = ch["resolved_link"]
            if link.startswith("ffmpeg "):
                link = link.split(" ")[1]

            # Formulate full URL with Kodi/VLC standard User-Agent header pipe
            full_link = f"{link}|User-Agent={quote_plus(USER_AGENT)}&Referer={quote_plus(ch['portal'])}/c/"

            formatted_name = f'({ch["server_name"]}) {ch["name"]}'

            f.write(f'#EXTINF:-1 tvg-name="{ch["name"]}" group-title="{ch["target"]}",{formatted_name}\n')

            # For standard VLC/IPTV players, provide the raw HTTP VLC OPT options so it sets HTTP headers
            f.write(f'#EXTVLCOPT:http-user-agent={USER_AGENT}\n')
            f.write(f'#EXTVLCOPT:http-referrer={ch["portal"]}/c/\n')

            f.write(f'{full_link}\n')

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
