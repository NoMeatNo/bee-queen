import json
import hashlib
import requests
import re
from urllib.parse import quote_plus
from collections import OrderedDict
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# We want these specific broadcasters as extracted from world_cup_2026.py
TARGET_CHANNELS = [
    "FOX", "FS1", "Telemundo", "Universo",
    "CTV", "TSN", "RDS",
    "Das Erste", "ZDF", "Magenta Sport",
    "M6", "beIN Sports",
    "BBC One", "BBC Two", "ITV 1", "ITV 4",
    "La 1", "Teledeporte", "Gol Mundial", "DAZN",
    "RAI 1", "RAI 2", "Rai Sport"
]

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
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
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
    print(f"[Handshake] Testing MAC {mac} for {portal_url}")
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, "")
    try:
        url = f"{portal_url}/server/load.php?type=stb&action=handshake&token=&Vs=1&vc=1"
        response = requests.get(url, headers=headers, cookies=cookies, timeout=5, verify=False)
        data = response.json()
        token = data.get("js", {}).get("token")
        random_value = data.get("js", {}).get("random", "0")
        if token:
            print(f"[Handshake] Success for MAC {mac}")

            # Activate session
            headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
            profile_url = f"{portal_url}/server/load.php?type=stb&action=get_profile"
            requests.get(profile_url, headers=headers, cookies=cookies, timeout=5, verify=False)

            return token, random_value
    except Exception as e:
        pass
    return None, None

def fetch_channels(portal_url, mac, token, random_value):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
    url = f"{portal_url}/server/load.php?type=itv&action=get_all_channels"
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=5, verify=False)
        data = response.json()
        if "js" in data and isinstance(data["js"], dict) and "data" in data["js"]:
            return data["js"]["data"]
    except Exception as e:
        print(f"Error fetching channels for {portal_url}: {e}")
    return []

def get_channel_link(portal_url, mac, token, random_value, cmd):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
    url = f"{portal_url}/server/load.php?type=itv&action=create_link&cmd={quote_plus(cmd)}"
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=5, verify=False)
        data = response.json()
        if "js" in data and isinstance(data["js"], dict) and "cmd" in data["js"]:
            return data["js"]["cmd"]
    except Exception as e:
        pass
    return None

def normalize_name(name):
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    return name.lower().strip()

def main():
    config = load_servers_config()
    all_channels_found = []

    for server in config.get("servers", []):
        portal_url = server.get("portal_url")
        server_name = server.get("name", "Unknown Server")
        if not portal_url:
            continue

        print(f"--- Processing {server_name} ({portal_url}) ---")
        token = None
        random_value = None
        working_mac = None

        for mac in server.get("macs", []):
            token, random_value = handshake(portal_url, mac)
            if token:
                working_mac = mac
                break

        if not token:
            print(f"No working MAC found for {portal_url}")
            continue

        print(f"Fetching channels for {portal_url}...")
        channels = fetch_channels(portal_url, working_mac, token, random_value)
        print(f"Found {len(channels)} channels.")

        for ch in channels:
            ch_name = ch.get("name", "")
            ch_norm = normalize_name(ch_name)

            for target in TARGET_CHANNELS:
                target_norm = normalize_name(target)
                if target_norm in ch_norm:
                    cmd = ch.get("cmd")
                    if cmd:
                        print(f"Found target channel: {ch_name} -> {target}")
                        all_channels_found.append({
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

    print(f"\nGenerating M3U file with {len(all_channels_found)} channels...")
    with open("hublive.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in all_channels_found:
            link = get_channel_link(ch["portal"], ch["mac"], ch["token"], ch["random"], ch["cmd"])
            if link:
                # Resolve link if it starts with ffmpeg or http
                if link.startswith("ffmpeg "):
                    link = link.split(" ")[1]
                formatted_name = f'({ch["server_name"]}) {ch["name"]}'
                f.write(f'#EXTINF:-1 tvg-name="{ch["name"]}" group-title="{ch["target"]}",{formatted_name}\n')
                f.write(f'{link}\n')
                print(f"Added: {formatted_name}")
            else:
                print(f"Failed to resolve link for: {ch['name']}")

    print("\nDone! M3U saved to hublive.m3u")

if __name__ == "__main__":
    main()
