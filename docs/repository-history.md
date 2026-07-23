# Repository history and evidence boundary

The public Git history starts with commit `86c451f`, a consolidated M1-M5 snapshot published on
2026-07-16. That first commit contains the API, worker, migrations, tests, evaluation scripts, and
documentation together. It does not expose the day-by-day implementation sequence and must not be
used as evidence that all of that work was produced in one uninterrupted step.

This limitation is retained honestly. The repository does not rewrite the published branch, invent
backdated commits, or fabricate pull requests and reviews. Reviewers should assess ownership and
engineering decisions from the current source, ADRs, executable tests, versioned reports, and
GitHub-hosted CI, then verify understanding through code discussion.

From the portfolio-credibility correction onward, changes are delivered as scoped commits whose
message and verification record describe one coherent outcome. This improves future reviewability
without pretending the original public history was more granular than it is.
