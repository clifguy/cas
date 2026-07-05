# Gamma Appendix: Observatory Guide-Camera Log Format

Appendix covering the guide-camera log format referenced by the alpha
observatory manual. Shares the observatory series code so the log format
travels with the manual it supports.

## Log Record Layout

Each guide-camera record carries a timestamp, centroid offset in
arcseconds, and an atmospheric seeing estimate computed from centroid
jitter over a rolling two-minute window.

## Retention

Guide logs are retained for one full observing season, then reduced to
nightly seeing summaries that feed the archive bundle manifests.
