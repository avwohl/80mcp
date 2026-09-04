import os, pty, select, time, fcntl, termios, signal
SB="/private/tmp/claude-501/-Users-wohl-src-80mcp/57f09b91-ea21-4a63-b89d-52ab64b3ee55/scratchpad/verify/t6"
os.makedirs(SB+"/xdg",exist_ok=True); os.chdir("/Users/wohl/src/romwbw_emu")
env=dict(os.environ); env["XDG_CONFIG_HOME"]=SB+"/xdg"
argv=["./src/romwbw_emu","--romwbw=roms/emu_romwbw.rom","--disk0=disks/hd1k_combo.img","--boot=2","--no-config"]
mfd,sfd=pty.openpty(); er,ew=os.pipe(); pid=os.fork()
if pid==0:
    os.setsid(); os.close(mfd); os.close(er)
    fcntl.ioctl(sfd,termios.TIOCSCTTY,0)
    os.dup2(sfd,0);os.dup2(sfd,1);os.dup2(ew,2);os.close(sfd);os.close(ew)
    os.execve(argv[0],argv,env); os._exit(127)
os.close(sfd); os.close(ew)
out=bytearray(); err=bytearray()
def pump(sec):
    end=time.time()+sec
    while time.time()<end:
        r,_,_=select.select([mfd,er],[],[],0.05)
        for f in r:
            try:d=os.read(f,65536)
            except OSError:d=b""
            (out if f==mfd else err).extend(d)
def wait(buf,needle,to=8.0):
    t0=time.time()
    while time.time()-t0<to:
        if needle.encode() in bytes(buf): return time.time()-t0
        pump(0.05)
    return None
wait(out,"A>"); os.write(mfd,b"\x05"); wait(err,"sim>")
def sim(c,to=3.0):
    n=len(err); os.write(mfd,c.encode()+b"\n")
    wait(err,"sim> ",to) if False else None
    t0=time.time()
    while time.time()-t0<to:
        pump(0.05)
        if bytes(err[n:]).rstrip().endswith(b"sim>"): break
    print(f"--- sim> {c}"); print(bytes(err[n:]).decode("latin-1").rstrip()); print()
sim("bp 0005")
# resume the guest, then wake it with a CR (the guest is BLOCKED on console input)
n=len(err); nout=len(out)
os.write(mfd,b"g\n"); pump(0.3)
os.write(mfd,b"\r")                       # this byte is GUEST input, not a sim command
t=wait(err,"Breakpoint hit",to=5); pump(0.4)
print(f"--- 'g' then one CR to the guest; trap reported after {t:.2f}s" if t else "--- NO TRAP")
print(bytes(err[n:]).decode("latin-1").rstrip()); print()
sim("r")            # NOW we are back at sim>, so this reaches the debugger
sim("dm 0000 32")
sim("dm 0005 16")
sim("dm D000 32")
sim("ba")
os.write(mfd,b"g\n"); pump(0.3)
try: os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)
except Exception: pass
