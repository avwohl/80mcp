import os, pty, select, sys, time, fcntl, termios, signal
SB = "/private/tmp/claude-501/-Users-wohl-src-80mcp/57f09b91-ea21-4a63-b89d-52ab64b3ee55/scratchpad/verify/t6"
os.makedirs(SB + "/xdg", exist_ok=True)
os.chdir("/Users/wohl/src/romwbw_emu")
env = dict(os.environ); env["XDG_CONFIG_HOME"] = SB + "/xdg"
argv = ["./src/romwbw_emu", "--romwbw=roms/emu_romwbw.rom",
        "--disk0=disks/hd1k_combo.img", "--boot=2", "--no-config"]
mfd, sfd = pty.openpty(); er, ew = os.pipe()
pid = os.fork()
if pid == 0:
    os.setsid(); os.close(mfd); os.close(er)
    fcntl.ioctl(sfd, termios.TIOCSCTTY, 0)
    os.dup2(sfd,0); os.dup2(sfd,1); os.dup2(ew,2); os.close(sfd); os.close(ew)
    os.execve(argv[0], argv, env); os._exit(127)
os.close(sfd); os.close(ew)
out = bytearray(); err = bytearray()
def pump(sec):
    end=time.time()+sec
    while time.time()<end:
        r,_,_=select.select([mfd,er],[],[],0.1)
        for f in r:
            try: d=os.read(f,65536)
            except OSError: d=b""
            (out if f==mfd else err).extend(d)
def wait_for(buf, needle, timeout=8.0):
    t0=time.time()
    while time.time()-t0<timeout:
        if needle.encode() in bytes(buf): return time.time()-t0
        pump(0.1)
    return None
wait_for(out,"A>")
os.write(mfd,b"\x05"); wait_for(err,"sim>")
def cmd(c, wait=0.8):
    n=len(err); os.write(mfd, c.encode()+b"\n"); pump(wait)
    print(f"--- sim> {c}"); print(bytes(err[n:]).decode("latin-1").rstrip()); print()
cmd("bp 0005")          # BDOS entry vector - every CP/M syscall goes through it
cmd("bl")
n=len(err); nout=len(out)
os.write(mfd, b"g\n")
# PACED: wait for the breakpoint report AND the re-issued sim> prompt before typing
t=wait_for(err,"Breakpoint hit",timeout=5); pump(0.5)
print("--- after 'g' (continue), waited %.2fs for the trap, STDERR: ---"%(t or -1))
print(bytes(err[n:]).decode("latin-1").rstrip()); print()
cmd("r")
cmd("s 3")
cmd("ba")
cmd("bl")
os.write(mfd,b"q\n"); pump(0.5)
try: os.kill(pid, signal.SIGKILL); os.waitpid(pid,0)
except Exception: pass
print("--- GUEST STDOUT emitted during the breakpoint sequence ---")
print(repr(bytes(out[nout:])[:300]))
