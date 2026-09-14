# GOAT Watch producer names

## Design and scope
The user requests that the banner name the person who produced the pallets at
that station. Live presence is not production credit, and the Recycling page's
presence map does not cover every station considered by GOAT Watch.

Use production_history.attribution_for for the selected day and current time,
which resolves the authoritative production matcher and covers all metered
stations. Read it once only when contenders exist. For each contender, show all
named people with positive credited units at that exact station, ordered by
credited units descending and name for ties. Keep names after departure. Ignore
zero-credit assignments. Support both legacy name keys and durable identity keys.
If attribution is unavailable or no named credit exists, show Producer unknown.
Never substitute the current worker or historical record holder.

Keep station selection, schedule eligibility, projection, record comparison,
record details, and persisted NEW GOAT alerts unchanged. Names use the normal
bold text style without live-presence coloring. No attendance writes are needed.

## Implementation
1. Add regression tests for departed producers, shared production, zero credit,
   station isolation (including Hand Build #1), missing attribution, and markup.
2. Add a producer-name lookup using shared attribution and attach names to the
   selected contenders. Keep compatibility with existing current-row callers.
3. Render producer names and an honest unknown fallback in the shared banner.
4. Run GOAT tests and relevant dashboard/production tests; inspect the diff.
5. Add a short What's New note, commit implementation, and push origin/main.

## Validation
The reproduced case is an eligible Hand Build #1 with no current operator rows
and positive credited production. Its banner must name its credited producer.
A replacement worker with no credited pallets must not replace the producer.
Existing record alerts and contender eligibility tests must continue to pass.
