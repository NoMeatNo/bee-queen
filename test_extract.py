import re
def extract_stream_id(cmd_val):
    cmd_val = str(cmd_val)
    print(f"Testing: {cmd_val}")
    m = re.search(r"stream=(\d+)", cmd_val)
    if m:
        print(f"Matched stream=(\d+): {m.group(1)}")
        return m.group(1)

    m2 = re.search(r'(?:/|^|ch/|cmd=)([0-9]+)(?:_.*)?$', cmd_val)
    if m2:
        print(f"Matched end of string: {m2.group(1)}")
        return m2.group(1)

    m3 = re.search(r"(\d+)", cmd_val)
    res = m3.group(1) if m3 else cmd_val
    print(f"Matched fallback: {res}")
    return res

extract_stream_id("ffmpeg http://127.0.0.1/ch/12345")
extract_stream_id("ffmpeg http://127.0.0.1/12345")
extract_stream_id("12345")
extract_stream_id("ffmpeg http://line.linehunt.org:8000/2085196")
extract_stream_id("http://line.linehunt.org:8000/play/live.php?mac=00:1A:79:C3:25:69&stream=2085196&extension=ts&play_token=6vUskiNoZX")
