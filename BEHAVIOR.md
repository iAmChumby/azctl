# azctl: What It Does

A behavioral handoff document. This describes everything the application does
from the point of view of the person using it. No code, no architecture, no
setup instructions. Just behavior.

---

## The one paragraph version

Azurite is a local storage emulator that developers run on their own machine so
they can build and test against fake Azure storage instead of the real thing. It
is actually three separate services running side by side (Blob, Queue, and
Table), and out of the box there is no single place to see whether they are up,
down, or broken. `azctl` is that place. It is a dashboard that lives in a
terminal window. It shows the health of all three services at once, lets you
start and stop them, and shows you their output as it happens.

---

## Who this is for and what it fixes

The person using this is a developer who runs Azurite as part of their day.

Before `azctl`, checking on Azurite meant one of these:

- Opening the code editor and hunting through an output panel.
- Installing a separate storage browsing application just to see if the thing
  is alive.
- Leaving three terminal windows open with logs scrolling past, and squinting
  at them.
- Guessing.

The worst version of this problem is when something else on the machine has
already claimed the ports Azurite wants. The developer's own services then fail
to start, and the reason is invisible. They lose twenty minutes to it. `azctl`
names the culprit and offers to remove it.

---

## What it deliberately does not do

This is not a data browser. It will never show you what is inside a container, a
queue, or a table. It does not read, write, or delete stored data, and it has no
opinion about what the data is.

It only manages whether the three services are running.

The practical effect of that boundary: nothing you can press in this application
can destroy your work. The worst outcome of any action is that a service stops,
and you can start it again.

---

## The four ways to use it

### 1. The dashboard

The main experience. You type the command and get a full-screen live view that
takes over the terminal. It updates on its own while you watch. This is the only
mode where you can change anything.

There is a variation that opens the dashboard and immediately starts all three
services, for the developer who wants one command in the morning and nothing
else.

### 2. A quick status check

Prints a snapshot of all three services and exits. Read-only. It changes
nothing, starts nothing, and stops nothing. Safe to run in any terminal at any
time, including while a dashboard is already open in another window.

### 3. A live status view

The same snapshot, but it keeps refreshing on its own until you stop it. Also
read-only. Meant for a second monitor or a side terminal that you glance at.

When you stop it, it tells you plainly that the services were left exactly as
they were.

### 4. Clearing the ports

A direct command for the "something else has my ports" problem. It finds every
process holding a Blob, Queue, or Table port, shows you a table naming each one
and its ID, and asks whether to kill them.

Because killing a process cannot be undone, it always asks first. There is a
flag to skip the question for people who already know, and an option to target
just one of the three ports instead of all of them.

After it kills something, it does not immediately declare victory. It waits a
moment and checks whether the ports actually came free, because a killed process
does not always let go instantly. If a port is still held after the wait, it
says so and tells you the likely reason: something automatically restarted it,
such as a code editor extension, and you need to stop it at the source.

---

## What you see on the dashboard

Four regions, top to bottom.

**The header** names the application and shows where the services are bound and
where the data lives. This is the "am I looking at the right thing" line.

**The status table** has one row per service and five columns:

| Column | What it tells you |
|---|---|
| Service | Blob, Queue, or Table. The selected one is marked. |
| Status | One of five states, described below. |
| Port | Which port this service is using. |
| PID | The process ID, or a dash when nothing is running. |
| Uptime | How long it has been up, in hours, minutes, seconds. |

**The log panel** shows the recent output of whichever service is selected. It
always shows the newest lines, and it grows or shrinks to fill whatever space
the terminal has. Making the window taller shows more history. Long lines are
cut off at the edge rather than wrapping, so one noisy line never eats three
rows.

If a service has never run, the panel says so and tells you what to press.

**The footer** holds the status legend, the action bar, the navigation hints,
and a message line where the application tells you what just happened.

---

## The five states a service can be in

This is the heart of the whole thing. Each state has a symbol and a colour so it
reads at a glance.

**Running.** Green. The service is up and answering. This is the state you want.

**Starting.** Yellow. The service was launched a moment ago but is not answering
yet. Normal for a few seconds.

**Stopped.** Grey. Nothing is running and nothing is on the port. Also normal;
it just means you have not started it.

**Broken.** Red. Something is wrong. This appears in two situations: the service
was launched and then died, or the service was launched and never came up. It
gives a service ten seconds to answer before declaring it broken, so a slow
start is not mistaken for a failure.

**Port in use.** Magenta. This is the important one. It means the dashboard is
not running this service, but something else on the machine is already sitting
on its port. This is the state that used to cost the developer twenty minutes of
confusion, and it is called out in its own colour precisely so it cannot be
mistaken for "running."

---

## How you drive it

The controls are deliberately unhurried. You do not memorise a dozen single
letters that fire immediately. You move a highlight to what you want and then
press Enter. Nothing disruptive can happen from a stray keystroke.

**Up and down arrows** choose which service you are working with. The chosen one
is marked in the table, and the log panel switches to it. Switching is instant.

**Left and right arrows** move along a row of named buttons at the bottom. The
highlighted one is boxed. Buttons that stop or kill something are printed in
red, so you can see the dangerous ones before you land on them.

**Enter** runs whatever is highlighted.

The buttons, in order:

| Button | What it does | Asks first? |
|---|---|---|
| Start | Starts the selected service | No |
| Stop | Stops the selected service | Yes |
| Restart | Stops then starts the selected service | Yes |
| Save | Writes the selected service's logs to a file | No |
| Free port | Kills whatever is holding the selected service's port | Yes, and names it |
| Start all | Starts all three | No |
| Stop all | Stops all three | Yes |

The last two are visually separated from the first five, because they act on
everything rather than on the one service you have selected.

---

## The confirmation behavior

Anything that stops or kills a process asks before doing it. The question
appears in the footer, in plain words, naming the exact thing about to happen
("Stop Blob?"). Enter confirms. Escape, or any other key, cancels and says
"Cancelled."

The Free port confirmation goes further. Before it kills anything it looks up
what is actually on that port and names it in the question: the process name and
its ID. So the developer is not confirming a blind kill, they are reading "kill
node.exe (PID 24188) on port 10000?" and deciding.

If nothing is on the port at all, Free port does not ask anything. It just says
nothing is listening there and moves on.

There is one honest edge case worth knowing: if the dashboard's own service is
the thing holding that port, Free port will kill it, and the dashboard correctly
notices its own process is gone and shows the service as stopped rather than
pretending it is still up.

---

## The combined log view

Pressing `a` toggles the log panel between two modes.

Normally it shows one service, the selected one.

Toggled on, it shows all three at once. Each line is tagged with its service and
printed in that service's colour: Blob in cyan, Queue in green, Table in
magenta. The lines are merged in the order they actually arrived, not grouped by
service, so you can see the real sequence of events across all three. This is
what you want when you are chasing a problem that crosses services.

The footer shows whether this mode is on or off, so you always know which view
you are reading.

---

## Saving logs

Each service keeps a rolling buffer of its recent output in memory, a few
thousand lines. The Save button writes that buffer out to a plain text file
named after the service, in whatever folder you launched from.

It then tells you exactly how many lines it wrote and the full path of the file,
so you can hand that file to someone else without hunting for it.

If there is nothing to save, it still writes the file and tells you it was
empty, rather than silently doing nothing and leaving you unsure whether the
button worked.

---

## Messages

When something happens, a short message appears at the bottom. Green when it
worked, red when it failed, yellow for "it worked but you should know
something," grey for routine notes like which service you just selected.

Messages fade after a few seconds so the footer does not accumulate stale text.

---

## Quitting

Pressing `q` quits, but only quietly if there is nothing running.

If services are still up, it stops and asks a three-way question, because there
are genuinely two reasonable answers:

- **Enter**: stop the services and exit. For when you are done for the day.
- **n**: leave the services running and exit. For when you just wanted the
  dashboard out of the way but your app still needs Azurite up.
- **Escape**: never mind, stay in the dashboard.

This matters because the dashboard is the owner of those services while it is
open. If it closed without asking, a developer would lose their running emulator
mid-task. The question exists so that never happens by accident.

If the terminal is closed or interrupted abruptly instead, the services are shut
down cleanly rather than being orphaned.

---

## When things go wrong

**The emulator is not installed on the machine.** Pressing Start does not crash
or hang. A red message appears telling the user the emulator is missing and
naming the exact command to install it.

**The supporting runtime is missing.** Same behavior: a clear red message rather
than a failure.

**A service is launched but never answers.** It sits at "starting" for ten
seconds, then flips to "broken" so the user is not left staring at a spinner
forever.

**A service dies on its own after starting.** It flips to "broken" on the next
refresh, without any user action, because the dashboard is continuously checking
rather than trusting what it did last.

**Something is respawning a killed process.** Free-ports detects this after the
kill and says so directly, including the likely cause, instead of reporting
success and letting the user discover the failure later.

---

## Two behaviors that look like bugs but are not

**Running the quick status check while the dashboard is open shows "port in
use," not "running."** This is correct and intentional. The status check is a
read-only observer that owns nothing. From where it stands, all it can honestly
say is "something is on this port, and it is not mine." Reporting "running"
would be claiming knowledge it does not have.

**Status and watch never change anything, ever.** Not even by accident. They
cannot start, stop, or kill. This is deliberate so that a developer can leave
one running in a side terminal, or run one in a hurry, with zero risk.

---

## The user stories

Written the way a stakeholder would want to read them back.

**As a developer starting my day**, I want one command that brings the whole
emulator up and shows me it is healthy, so I can get to work without a ritual.

**As a developer whose app cannot reach storage**, I want to see at a glance
which of the three services is down, so I stop guessing which layer is broken.

**As a developer whose service will not start**, I want to be told that
something else owns the port and what that something is by name, so I fix the
real problem instead of restarting things at random.

**As a developer who needs that port back**, I want to remove the squatter from
the same screen I found it on, after being shown exactly what I am about to
kill, so it is fast but not reckless.

**As a developer chasing a bug across services**, I want to read all three logs
merged together in the order things actually happened, colour-coded so I can
tell them apart.

**As a developer asking a colleague for help**, I want to save what a service
printed to a file in one keystroke, and be told where that file is.

**As a developer who just wants to check on things**, I want a read-only view I
can run anywhere, in any terminal, without any chance of disturbing what is
running.

**As a developer closing my laptop**, I want to be asked whether to shut the
emulator down or leave it running, because both are legitimate and only I know
which one I meant.

**As a developer generally**, I want to know that nothing in this tool can touch
my stored data, so I can press buttons without reading the manual first.
