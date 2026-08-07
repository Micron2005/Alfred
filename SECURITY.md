# Security posture

Honest version, because a false sense of security is worse than none.

## What is actually achievable

No system is "unhackable" — anyone who claims that is selling something. What
IS achievable: make Micron OS a hard target, so a casual attacker moves on and
a serious one has to work and gets noticed. That is the real goal, and it is
worth doing.

Security here is layers, not a password box:

1. THE DOOR — Alfred binds to localhost by default. Nothing off the machine
   can reach him unless you deliberately open it. Most "hacking" is remote;
   a service that does not listen to the network cannot be reached by it.

2. THE GATE — os.apply already requires your approval and refuses to touch
   ssh, sudo, systemd, ~/.ssh. Even a compromised model cannot silently
   reconfigure the machine. This is the single most important control and it
   is already built.

3. THE HANDS ARE TIED — workers run scoped: passwordless sudo is limited to
   apt-get and systemctl, never ALL. The blast radius of any single action
   is bounded by design.

4. THE OS ITSELF — Ubuntu with automatic security updates and its firewall
   on carries most of the weight. Alfred is a small attack surface; the OS
   underneath him is the real perimeter, and it is maintained by thousands
   of people who do this full time.

5. SECRETS STAY OUT OF REACH — API keys (Onshape, etc.) live in the
   environment, never in the repo, never in the shell, never in a JS file a
   browser could leak.

## What a password should and should not do

A login on the SHELL is worth having — it stops someone who walks up to an
unlocked terminal from talking to Alfred as you. But it must be:
  - checked on the SERVER, never in browser JavaScript (which anyone bypasses)
  - a salted hash on disk, never the password itself
  - understood for what it is: a lock on the front door, not armor. It does
    not encrypt anything and does not stop a determined remote attacker. The
    localhost bind and the OS firewall do that.

## Viruses

On Linux, for a single-user home machine, the honest picture: drive-by
viruses are rare compared to Windows, and the real risks are (a) running
untrusted downloaded scripts as sudo, and (b) an unpatched network service.
Micron OS addresses both — it does not download-and-run anything on its own,
and it keeps the network surface tiny. Antivirus (clamav) is available but is
not the main event; updates and a small attack surface are.

## The rule that outranks all of this

The owner is never locked out of his own house. Every control here fails
OPEN for the owner and CLOSED for everyone else. A security measure that
could permanently lock you out of your own machine is a bug, not a feature.
