import os,pty,select,time,fcntl,termios,signal
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
def pump(s):
    e=time.time()+s
    while time.time()<e:
        r,_,_=select.select([mfd,er],[],[],0.05)
        for f in r:
            try:d=os.read(f,65536)
            except OSError:d=b""
            (out if f==mfd else err).extend(d)
def wait(b,n,to=8.0):
    t0=time.time()
    while time.time()-t0<to:
        if n.encode() in bytes(b): return time.time()-t0
        pump(0.05)
wait(out,"A>"); os.write(mfd,b"\x05"); wait(err,"sim>"); pump(0.3)
def sim(c,to=2.0):
    n=len(err); os.write(mfd,c.encode()+b"\n"); t0=time.time()
    while time.time()-t0<to:
        pump(0.05)
        if bytes(err[n:]).rstrip().endswith(b"sim>"): break
    print(f"--- sim> {c}"); print(bytes(err[n:]).decode("latin-1").rstrip())
# Smash the ENTIRE low 256 bytes: warm-boot vector, BDOS vector, everything.
sim("d 0000 FF FF FF FF FF FF FF FF")
sim("dm 0000 8")
nout=len(out)
os.write(mfd,b"g\n"); pump(0.4)
os.write(mfd,b"DIR\r"); pump(2.5)
guest=bytes(out[nout:]).decode("latin-1")
print("\n--- after smashing 0000-0007 via the debugger, the guest was told DIR: ---")
print(guest[:600])
print("\nVERDICT:", "guest UNAFFECTED -> debugger memory is NOT the guest's memory"
      if "COM" in guest else "guest crashed/changed -> debugger memory IS the guest's")
try: os.kill(pid,signal.SIGKILL); os.waitpid(pid,0)
except Exception: pass
