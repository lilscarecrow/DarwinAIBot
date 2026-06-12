"""
Run this while Darwin Project is open (any screen) to find the actual process name.
Usage:  python debug_processes.py
"""
import psutil

print("Running processes (filtered for likely game/Darwin entries):\n")
all_procs = []
for proc in psutil.process_iter(["pid", "name", "exe"]):
    try:
        name = proc.info["name"] or ""
        exe = proc.info["exe"] or ""
        all_procs.append((name.lower(), name, proc.info["pid"], exe))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

# Show anything that looks game-related
keywords = ["darwin", "epic", "steam", "unreal", "shipping", "game"]
matches = [p for p in all_procs if any(k in p[0] for k in keywords)]

if matches:
    print(f"{'PID':<8} {'Name':<45} Exe")
    print("-" * 100)
    for _, name, pid, exe in sorted(matches, key=lambda x: x[1]):
        print(f"{pid:<8} {name:<45} {exe}")
else:
    print("No obvious game processes found. Is the game open?")

print("\n--- All processes (alphabetical) ---")
for _, name, pid, exe in sorted(all_procs, key=lambda x: x[1]):
    print(f"{pid:<8} {name}")
