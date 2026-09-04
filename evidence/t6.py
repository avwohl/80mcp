import os, pty, select, sys, time, fcntl, termios, signal

SB = "/private/tmp/claude-501/-Users-wohl-src-80mcp/57f09b91-ea21-4a63-b89d-52ab64b3ee55/scratchpad/verify/t6"
os.makedirs(SB + "/xdg", exist_ok=True)
os.chdir("/Users/wohl/src/romwbw_emu")
env = dict(os.environ)
env["XDG_CONFIG_HOME"] = SB + "/xdg"
argv = ["./src/romwbw_emu", "--romwbw=roms/emu_romwbw.rom",
        "--disk0=disks/hd1k_combo.img", "--boot=2", "--no-config"]

mfd, sfd = pty.openpty()
er, ew = os.pipe()
pid = os.fork()
if pid == 0:
    os.setsid(); os.close(mfd); os.close(er)
    fcntl.ioctl(sfd, termios.TIOCSCTTY, 0)
    os.dup2(sfd, 0); os.dup2(sfd, 1); os.dup2(ew, 2)
    os.close(sfd); os.close(ew)
    os.execve(argv[0], argv, env); os._exit(127)
os.close(sfd); os.close(ew)

out = bytearray(); err = bytearray()
def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        r, _, _ = select.select([mfd, er], [], [], 0.1)
        for f in r:
            try: d = os.read(f, 65536)
            except OSError: d = b""
            (out if f == mfd else err).extend(d)

def wait_for(chan, needle, timeout=8.0):
    """Wait until needle appears in chan ('out'|'err'). Returns elapsed or None."""
    t0 = time.time()
    buf = out if chan == "out" else err
    while time.time() - t0 < timeout:
        if needle.encode() in bytes(buf):
            return time.time() - t0
        pump(0.1)
    return None

t = wait_for("out", "A>")
print(f"[harness] boot to CP/M 'A>' prompt on the pty: {t:.2f}s" if t else "[harness] NO A> PROMPT")

print("[harness] sending 0x05 (^E) to enter console mode")
os.write(mfd, b"\x05")
t = wait_for("err", "sim>")
print(f"[harness] 'sim> ' seen ON STDERR after {t:.2f}s" if t else "[harness] NO sim> PROMPT")

mark = {}
for cmd in ["r", "dm 0100", "bp", "help"]:
    before = len(err)
    os.write(mfd, cmd.encode() + b"\n")
    pump(0.8)
    mark[cmd] = bytes(err[before:])

os.write(mfd, b"q\n"); pump(0.5)
os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)

for cmd in ["r", "dm 0100", "bp", "help"]:
    print(f"\n===== sim> {cmd}  (reply on STDERR) =====")
    print(mark[cmd].decode("latin-1").rstrip())

print("\n===== last 200 bytes of GUEST STDOUT (pty), for channel separation =====")
print(repr(bytes(out)[-200:]))
