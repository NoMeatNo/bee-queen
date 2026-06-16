import json
import hashlib
import requests
import re
import concurrent.futures
from urllib.parse import quote_plus, urlparse
import urllib3
import sys

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Target channel
TARGET_CHANNELS = ["ITV 1", "ITV1"]

# Quality filtering
QUALITY_FILTER = "hevc"

# Handshake timeout and attempts config
HANDSHAKE_TIMEOUT = 5.0
FETCH_TIMEOUT = 10.0
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

    adid = hashlib.md5((device_id2 + mac_upper).encode()).hexdigest()
    return {"sn": serialnumber, "device_id": serialnumber, "device_id2": device_id2, "adid": adid}

def build_auth_headers_and_cookies(portal_url, mac, token, random_value="0"):
    mac_upper = (mac or "").strip().upper()
    identity = build_device_identity(mac_upper)
    parsed = urlparse(portal_url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "X-User-Agent": "Model: MAG250; Link:",
        "Referer": f"{portal_url}/c/",
        "Host": parsed.netloc,
        "Authorization": f"Bearer {token}" if token else "",
    }

    cookies = {
        "mac": mac_upper,
        "stb_lang": "en",
        "timezone": "Europe/Bucharest",
        "sn": identity["sn"],
        "device_id": identity["device_id"],
        "device_id2": identity["device_id2"],
        "adid": identity["adid"],
        "hw_version": "1.7-BD-00",
    }
    if random_value:
        cookies["random"] = random_value
    return headers, cookies

def handshake(portal_url, mac):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, "")

    url = f"{portal_url.rstrip('/')}/portal.php?type=stb&action=handshake&JsHttpRequest=1-xml"
    params = {"token": ""}

    try:
        response = requests.get(url, headers=headers, cookies=cookies, params=params, timeout=HANDSHAKE_TIMEOUT, verify=False)
        data = response.json()
        token = data.get("js", {}).get("token")
        random_value = data.get("js", {}).get("random", "0")

        if token:
            headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)
            profile_url = f"{portal_url.rstrip('/')}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml"
            requests.get(profile_url, headers=headers, cookies=cookies, timeout=HANDSHAKE_TIMEOUT, verify=False)
            return token, random_value
    except Exception:
        pass

    return None, None

def fetch_channels(portal_url, mac, token, random_value):
    headers, cookies = build_auth_headers_and_cookies(portal_url, mac, token, random_value)

    urls_to_try = [
        f"{portal_url}/server/load.php?type=itv&action=get_all_channels",
        f"{portal_url}/portal.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"
    ]
    for url in urls_to_try:
        try:
            response = requests.get(url, headers=headers, cookies=cookies, timeout=FETCH_TIMEOUT, verify=False)
            data = response.json()
            if "js" in data and isinstance(data["js"], dict) and "data" in data["js"]:
                return data["js"]["data"]
        except Exception:
            continue
    return []

def _extract_play_token(returned_cmd):
    play_token_match = re.search(r"play_token=([a-zA-Z0-9]+)", returned_cmd or "")
    if play_token_match:
        return play_token_match.group(1)
    return None

def extract_stream_id(ch):
    cmd_val = str(ch.get("cmd", ""))

    m = re.search(r"stream=(\d+)", cmd_val)
    if m: return m.group(1)

    if ch.get("id"): return str(ch["id"])
    if ch.get("stream_id"): return str(ch["stream_id"])

    m2 = re.search(r'(?:/|^|ch/|cmd=)([0-9]+)(?:_.*)?$', cmd_val)
    if m2: return m2.group(1)

    m3 = re.search(r"(\d+)", cmd_val)
    return m3.group(1) if m3 else cmd_val

def get_channel_link(ch):
    headers, cookies = build_auth_headers_and_cookies(ch["portal"], ch["mac"], ch["token"], ch["random"])

    # We send EXACTLY the cmd or id to create_link like Kodi does
    cmd_val = ch.get("cmd") or ch.get("id") or ch.get("stream_id")

    urls_to_try = [
        f"{ch['portal'].rstrip('/')}/server/load.php?type=itv&action=create_link&cmd={quote_plus(str(cmd_val))}",
        f"{ch['portal'].rstrip('/')}/portal.php?type=itv&action=create_link&cmd={quote_plus(str(cmd_val))}&JsHttpRequest=1-xml"
    ]

    for create_link_url in urls_to_try:
        try:
            response = requests.get(create_link_url, headers=headers, cookies=cookies, timeout=FETCH_TIMEOUT, verify=False)
            data = response.json()
            if "js" in data and isinstance(data["js"], dict) and "cmd" in data["js"]:
                returned_cmd = data["js"]["cmd"]

                if returned_cmd:
                    if returned_cmd.startswith("http"):
                        fixed_url = returned_cmd
                        if "&stream=&" in fixed_url:
                            final_stream_id = extract_stream_id(ch)
                            fixed_url = fixed_url.replace("&stream=&", f"&stream={final_stream_id}&")

                        try:
                            probe_response = requests.get(fixed_url, headers=headers, stream=True, timeout=5.0, verify=False, allow_redirects=True)
                            if probe_response.url and probe_response.status_code < 400:
                                return ch, probe_response.url
                        except Exception:
                            pass
                        return ch, fixed_url

                    if returned_cmd.startswith("ffmpeg "):
                        if "http" in returned_cmd:
                            return ch, returned_cmd.split("ffmpeg ")[1].strip()
                        return ch, returned_cmd

                play_token = _extract_play_token(returned_cmd)
                if play_token:
                    final_stream_id = extract_stream_id(ch)
                    initial_url = f"{ch['portal'].rstrip('/')}/play/live.php?mac={ch['mac']}&stream={final_stream_id}&extension=ts&play_token={play_token}"

                    try:
                        probe_headers = headers.copy()
                        probe_headers.update({
                            "Accept-Encoding": "identity",
                            "Connection": "close"
                        })
                        probe_response = requests.get(initial_url, headers=probe_headers, stream=True, timeout=5.0, verify=False, allow_redirects=True)
                        if probe_response.url and probe_response.status_code < 400:
                            return ch, probe_response.url
                    except Exception:
                        pass

                    return ch, initial_url

        except Exception:
            pass

    final_stream_id = extract_stream_id(ch)
    fallback_url = f"{ch['portal'].rstrip('/')}/play/live.php?mac={ch['mac']}&stream={final_stream_id}&extension=ts"
    try:
        probe_response = requests.get(fallback_url, headers=headers, stream=True, timeout=5.0, verify=False, allow_redirects=True)
        if probe_response.url and probe_response.status_code < 400:
            return ch, probe_response.url
    except Exception:
        pass

    return ch, fallback_url

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
                        "id": ch.get("id"),
                        "stream_id": ch.get("stream_id"),
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
