# Incidents behind the checks

Each check is here because its failure happened. The incidents come from one developer's
portfolio of about fifteen repositories, mostly written with coding agents, between June
and September 2026. The numbers are as measured at the time. Read these to explain a check
to someone who thinks it's overkill, and to recognise the failure when it starts again.

## Contents
- The dead auth system that took three rounds to find
- Two AuthProviders, one live
- "One function, two callers" with zero callers
- A test named for the thing it never imports
- A decoder held up by a test that never runs
- The allowlist that went stale
- "It can gate a deploy", and nothing ran it
- Twelve failures that were the probe, not the code
- The checker that accused an honest commit six times
- The edit that "succeeded" and changed nothing

---

### The dead auth system that took three rounds to find
A production RAG SaaS deleted 36 files that nothing imported. That orphaned nine more,
which were an entire dead auth subsystem (a factory, two providers, a JWT session bridge,
a unified hook and its types), kept alive only by other dead code. A third round orphaned
two more. None of it was reachable from the running app. A single-pass scan finds only
the first layer.
**Check:** the orphan scan repeats until nothing new appears, and reports each round.

### Two AuthProviders, one live
In the same codebase, `components/auth-provider.tsx` exported `AuthProvider`, and the live
one was in `contexts/AuthContext.tsx`. `hooks/use-auth-unified.ts` exported `useAuth` and
`useTenant`, and the live ones were elsewhere. Deleting by name would have removed the wrong
twin and broken sign-in. An agent asked to "fix AuthProvider" can edit the dead copy, see
nothing change, and try again.
**Check:** twins are reported with the copy that's reachable from an entry point, and
deletion is by reference, never by name.

### "One function, two callers" with zero callers
A contract was documented as "one predicate, two callers". It had zero callers. Its tests
passed the whole time, because they tested the function and never checked that anything
called it.
**Check:** a "wired" or "called by" claim is traced from a real entry point.

### A test named for the thing it never imports
Tests named after a component imported only that component's dependencies, never the
component. They could not fail if the component broke.
**Check:** `tests-never-import-changed`, and the recipe's "break it on purpose" step.

### A decoder held up by a test that never runs
A base64 decoder looked live because a test imported it. That test suite was excluded from
the runner as never-ported Jest. The only thing keeping the file "in use" was a test nobody
ran.
**Check:** the orphan scan's "kept alive only by tests" list; the recipe's "count what
ran, not what's written".

### The allowlist that went stale
The same repo had a guard listing the files it knew were unreferenced, 22 of them. Run
again on 2026-09-27, the orphan scan found all 22 plus 10 more: a second, abandoned
`lib` tree, duplicate hooks, and components kept alive only by other orphans. An allowlist that
isn't re-derived becomes a list of what used to be true.
**Check:** re-run the scan; never trust a list of "known dead" without regenerating it.

### "It can gate a deploy", and nothing ran it
A marketplace repo's documentation checker said in its own header that it "exits non-zero
so it can gate a deploy". No CI job, hook or build step ran it. The next day, measured: it
covered 14 of 48 documents, and the other 34 held 26 dead pointers.
**Check:** "can" is not "does". A claim that something is enforced is verified by finding
what runs it.

### Twelve failures that were the probe, not the code
In a browser game project, twelve times a check failed right after a change that measured
well, and every time the check was wrong, not the product. Examples: a probe that read a
button's label from `textContent` (spans joined with no space) and searched for it with an
accessible-name lookup (spans joined with a space), so a correct change broke the probe.
**Check:** before giving "Doesn't hold", confirm the instrument. Re-run the evidence
another way.

### The checker that accused an honest commit six times
The first real run of this skill's own script, on a carefully written deletion commit,
marked six claims CONTRADICTED. Every one was a file the message named as context ("the
live ones are…", "none of it is touched here", "Kept: …"), not a claim that it changed. A
check that cries wolf on honest work gets switched off.
**Check:** a path counts as "claimed changed" only when a change verb governs it in the
same clause. The script is a locator: nothing it prints is a verdict until the code is read.

### The edit that "succeeded" and changed nothing
Scripted edits during this skill's own construction reported success while matching
nothing, several times: shell quoting ate escape characters, and a replace targeted text
that wasn't there. A green "done" from a tool is a claim like any other.
**Check:** assert that each scripted edit changed the file, and break each new check once
on purpose to confirm it fires.
