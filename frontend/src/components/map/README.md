# `components/map`

## Responsibility

The operational picture: a site-centred PPI in SVG. Draws range rings, zones, sensor placements and track markers in the site's local ENU frame, in metres.

## Interface boundary

Renders backend state. Computes no track and decides no track state. Range and bearing shown here are derived from geodetic position relative to the *site*, and are labelled as such because no sensor measured them. Coverage arcs are absent because the API cannot say what they are.
