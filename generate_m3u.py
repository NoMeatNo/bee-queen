import json
import hashlib
import requests
import re
import concurrent.futures
from urllib.parse import quote_plus
import urllib3
import sys

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Test target channel
TARGET_CHANNELS = ["ITV 1", "ITV1"]

# Quality filtering: If set to a string (e.g. "HEVC", "FHD", "HD"), it will only keep channels
# whose names contain that string (case-insensitive). If set to "" or None, it extracts all matches.
QUALITY_FILTER = "hevc"

# Handshake timeout and attempts config
HANDSHAKE_TIMEOUT = 3.0
FETCH_TIMEOUT = 5.0
MAX_MAC_ATTEMPTS = 5

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

def get_channel_link(ch):
    headers, cookies = build_auth_headers_and_cookies(ch["portal"], ch["mac"], ch["token"], ch["random"])
    url = f"{ch['portal']}/server/load.php?type=itv&action=create_link&cmd={quote_plus(ch['cmd'])}"
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=FETCH_TIMEOUT, verify=False)
        data = response.json()
        if "js" in data and isinstance(data["js"], dict) and "cmd" in data["js"]:
            return ch, data["js"]["cmd"]
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

    print(f"--- Checking {server_name} ({portal_url}) ---")
    token = None
    random_value = None
    working_mac = None

    attempts = 0
    # Process MACs concurrently for faster handshake
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        macs_to_test = server.get("macs", [])[:MAX_MAC_ATTEMPTS] # Test up to N MACs to avoid hanging on dead servers

        future_to_mac = {executor.submit(handshake, portal_url, mac): mac for mac in macs_to_test}
        for future in concurrent.futures.as_completed(future_to_mac):
            mac = future_to_mac[future]
            try:
                t, r = future.result()
                if t:
                    token = t
                    random_value = r
                    working_mac = mac
                    # Cancel other futures if possible by stopping the loop, but we can't cleanly abort others easily
                    # We just take the first working one
                    break
            except Exception:
                pass

    if not token:
        print(f"No working MAC found for {portal_url} (checked {len(macs_to_test)} macs)")
        return []

    print(f"Fetching channels for {portal_url}...")
    channels = fetch_channels(portal_url, working_mac, token, random_value)
    print(f"Found {len(channels)} channels on {server_name}.")

    server_channels = []
    for ch in channels:
        ch_name = ch.get("name", "")

        # Apply quality filter before heavy matching
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

    print(f"Discovering channels across all servers (Quality Filter: '{QUALITY_FILTER}')...")

    # Run the server processing in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_server = {executor.submit(process_server, server): server for server in config.get("servers", [])}
        for future in concurrent.futures.as_completed(future_to_server):
            server_channels = future.result()
            all_channels_found.extend(server_channels)

    if not all_channels_found:
        print("No matching channels found.")
        sys.exit(0)

    # Parallel resolve links
    print(f"\nResolving links for {len(all_channels_found)} channels in parallel...")
    resolved_channels = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_ch = {executor.submit(get_channel_link, ch): ch for ch in all_channels_found}
        for future in concurrent.futures.as_completed(future_to_ch):
            ch, link = future.result()
            if link:
                ch["resolved_link"] = link
                resolved_channels.append(ch)
                print(f"Resolved: ({ch['server_name']}) {ch['name']}")
            else:
                print(f"Failed to resolve link for: {ch['name']}")

    # Write M3U
    print(f"\nGenerating M3U file with {len(resolved_channels)} channels...")
    with open("hublive.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in resolved_channels:
            link = ch["resolved_link"]
            # Resolve link if it starts with ffmpeg or http
            if link.startswith("ffmpeg "):
                link = link.split(" ")[1]
            formatted_name = f'({ch["server_name"]}) {ch["name"]}'
            f.write(f'#EXTINF:-1 tvg-name="{ch["name"]}" group-title="{ch["target"]}",{formatted_name}\n')
            f.write(f'{link}\n')

    print("\nDone! M3U saved to hublive.m3u")

if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
