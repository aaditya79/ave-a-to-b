# Expected answers for the sample queries

Each number below is what the agent answered on the live site (docs/traces.json). Under it is the file it comes
from and a DuckDB query that recomputes it from the raw Parquet files, without any project code. Run the queries
from the repo root, in the DuckDB command line (`duckdb`) or in Python with `duckdb.sql(query).df()`.
`docs/sample_routes.csv` lists every segment of each route, in order, as routed on the live site.

A range is n ± 2 × √n, floored at 0.

## Query 1: I bike from Astoria, Queens to Times Square. Which route should I take?

**Minutes and risk compared with the Fastest route, at the half, assumed, and double rider-share settings**

- Agent's answer: fastest: 32.1 min, 1.0 / 1.0 / 1.0; balanced: 33.7 min, 0.46 / 0.39 / 0.38; lowest_risk: 35.1 min, 0.52 / 0.38 / 0.27. Recomputed by the query: matches.
- Source: docs/sample_routes.csv, data/edges.parquet (travel_min, route_risk_half, route_risk_assumed, route_risk_double)

```sql
SELECT r.route,
       round(sum(e.travel_min), 1) AS minutes,
       round(sum(e.route_risk_half) / max(f.half), 2) AS risk_vs_fastest_half,
       round(sum(e.route_risk_assumed) / max(f.assumed), 2) AS risk_vs_fastest_assumed,
       round(sum(e.route_risk_double) / max(f.double), 2) AS risk_vs_fastest_double
FROM 'docs/sample_routes.csv' r
JOIN 'data/edges.parquet' e USING (edge_id)
CROSS JOIN (SELECT sum(route_risk_half) AS half, sum(route_risk_assumed) AS assumed, sum(route_risk_double) AS double
            FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' USING (edge_id)
            WHERE query = 1 AND route = 'fastest') f
WHERE r.query = 1
GROUP BY r.route ORDER BY minutes;
```

**Safety label 'about_equally_safe' and recommended route 'balanced'. Rule: 'safest' needs the lowest-risk route's range (risk × (n ± 2 × √n) / n) to sit below every other route's range at all three settings; overlapping ranges mean 'about equally safe', and the faster of the tied routes is recommended. The query shows the assumed setting; swap in route_risk_half and route_risk_double for the other two.**

- Agent's answer: about_equally_safe.
- Source: docs/sample_routes.csv, data/edges.parquet (route_risk_*, crash_blended_rate, mile_years)

```sql
SELECT route,
       round(sum(e.route_risk_assumed), 2) AS risk,
       round(sum(e.crash_blended_rate * e.mile_years), 1) AS n,
       round(sum(e.route_risk_assumed) * greatest(0, n - 2 * sqrt(n)) / n, 2) AS risk_low,
       round(sum(e.route_risk_assumed) * (n + 2 * sqrt(n)) / n, 2) AS risk_high
FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' e USING (edge_id)
WHERE query = 1
GROUP BY route ORDER BY risk;
```

**Stretch 1 that stands out: Central Park South (segment 42430394-4347534767-0). It stands out because range_low is above similar_streets_expected.**

- Agent's answer: 10 crashes (range 3.7 to 16.3), about 2.9 expected on similar streets, main cause Driver Inattention/Distraction. Recomputed by the query: matches.
- Source: data/crash_locations.parquet (edge_id), data/crashes.parquet (contributing_factor_vehicle_1), data/edges.parquet (crash_class_rate, mile_years)

```sql
SELECT count(*) AS crashes_since_2021,
       CASE WHEN count(*) < 10 THEN [0.0, 0.0, 0.2, 0.6, 1.1, 1.6, 2.2, 2.8, 3.5, 4.1][count(*) + 1] ELSE round(count(*) - 2 * sqrt(count(*)), 1) END AS range_low,
       CASE WHEN count(*) < 10 THEN [3.7, 5.6, 7.2, 8.8, 10.2, 11.7, 13.1, 14.4, 15.8, 17.1][count(*) + 1] ELSE round(count(*) + 2 * sqrt(count(*)), 1) END AS range_high,
       (SELECT round(crash_class_rate * mile_years, 1) FROM 'data/edges.parquet' WHERE edge_id = '42430394-4347534767-0') AS similar_streets_expected,
       (SELECT contributing_factor_vehicle_1 FROM 'data/crash_locations.parquet' l2 JOIN 'data/crashes.parquet' c2 USING (collision_id)
        WHERE l2.edge_id = '42430394-4347534767-0' AND contributing_factor_vehicle_1 NOT IN ('Unspecified', 'Does Not Apply')
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1) AS main_cause
FROM 'data/crash_locations.parquet' l JOIN 'data/crashes.parquet' c USING (collision_id)
WHERE l.edge_id = '42430394-4347534767-0';
```

**One police report behind stretch 1, in the city's own copy of the crash table. Its street names should match Central Park South.**

- Agent's answer: collision_id 2e678859c5e01d4406fa44622f081c7b.
- Source: NYC Open Data, Motor Vehicle Collisions - Crashes (h9gi-nx95)

```sql
-- open in a browser:
-- https://data.cityofnewyork.us/resource/h9gi-nx95.json?collision_id=2e678859c5e01d4406fa44622f081c7b
```

## Query 2: I bike from Columbia University to Union Square, Manhattan around 8am. Which route should I take?

**Minutes and risk compared with the Fastest route, at the half, assumed, and double rider-share settings**

- Agent's answer: fastest: 35.5 min, 1.0 / 1.0 / 1.0; balanced: 39.0 min, 0.53 / 0.38 / 0.3; lowest_risk: 42.2 min, 0.42 / 0.31 / 0.25. Recomputed by the query: matches.
- Source: docs/sample_routes.csv, data/edges.parquet (travel_min, route_risk_half, route_risk_assumed, route_risk_double)

```sql
SELECT r.route,
       round(sum(e.travel_min), 1) AS minutes,
       round(sum(e.route_risk_half) / max(f.half), 2) AS risk_vs_fastest_half,
       round(sum(e.route_risk_assumed) / max(f.assumed), 2) AS risk_vs_fastest_assumed,
       round(sum(e.route_risk_double) / max(f.double), 2) AS risk_vs_fastest_double
FROM 'docs/sample_routes.csv' r
JOIN 'data/edges.parquet' e USING (edge_id)
CROSS JOIN (SELECT sum(route_risk_half) AS half, sum(route_risk_assumed) AS assumed, sum(route_risk_double) AS double
            FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' USING (edge_id)
            WHERE query = 2 AND route = 'fastest') f
WHERE r.query = 2
GROUP BY r.route ORDER BY minutes;
```

**Safety label 'about_equally_safe' and recommended route 'balanced'. Rule: 'safest' needs the lowest-risk route's range (risk × (n ± 2 × √n) / n) to sit below every other route's range at all three settings; overlapping ranges mean 'about equally safe', and the faster of the tied routes is recommended. The query shows the assumed setting; swap in route_risk_half and route_risk_double for the other two.**

- Agent's answer: about_equally_safe.
- Source: docs/sample_routes.csv, data/edges.parquet (route_risk_*, crash_blended_rate, mile_years)

```sql
SELECT route,
       round(sum(e.route_risk_assumed), 2) AS risk,
       round(sum(e.crash_blended_rate * e.mile_years), 1) AS n,
       round(sum(e.route_risk_assumed) * greatest(0, n - 2 * sqrt(n)) / n, 2) AS risk_low,
       round(sum(e.route_risk_assumed) * (n + 2 * sqrt(n)) / n, 2) AS risk_high
FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' e USING (edge_id)
WHERE query = 2
GROUP BY route ORDER BY risk;
```

**Stretch 1 that stands out: 7th Avenue (segment 42439952-42439955-0). It stands out because range_low is above similar_streets_expected.**

- Agent's answer: 7 crashes (range 1.7 to 12.3), about 1.4 expected on similar streets, main cause Traffic Control Disregarded. Recomputed by the query: matches.
- Source: data/crash_locations.parquet (edge_id), data/crashes.parquet (contributing_factor_vehicle_1), data/edges.parquet (crash_class_rate, mile_years)

```sql
SELECT count(*) AS crashes_since_2021,
       CASE WHEN count(*) < 10 THEN [0.0, 0.0, 0.2, 0.6, 1.1, 1.6, 2.2, 2.8, 3.5, 4.1][count(*) + 1] ELSE round(count(*) - 2 * sqrt(count(*)), 1) END AS range_low,
       CASE WHEN count(*) < 10 THEN [3.7, 5.6, 7.2, 8.8, 10.2, 11.7, 13.1, 14.4, 15.8, 17.1][count(*) + 1] ELSE round(count(*) + 2 * sqrt(count(*)), 1) END AS range_high,
       (SELECT round(crash_class_rate * mile_years, 1) FROM 'data/edges.parquet' WHERE edge_id = '42439952-42439955-0') AS similar_streets_expected,
       (SELECT contributing_factor_vehicle_1 FROM 'data/crash_locations.parquet' l2 JOIN 'data/crashes.parquet' c2 USING (collision_id)
        WHERE l2.edge_id = '42439952-42439955-0' AND contributing_factor_vehicle_1 NOT IN ('Unspecified', 'Does Not Apply')
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1) AS main_cause
FROM 'data/crash_locations.parquet' l JOIN 'data/crashes.parquet' c USING (collision_id)
WHERE l.edge_id = '42439952-42439955-0';
```

**One police report behind stretch 1, in the city's own copy of the crash table. Its street names should match 7th Avenue.**

- Agent's answer: collision_id 628498aae7b24fe266df5356047b86bd.
- Source: NYC Open Data, Motor Vehicle Collisions - Crashes (h9gi-nx95)

```sql
-- open in a browser:
-- https://data.cityofnewyork.us/resource/h9gi-nx95.json?collision_id=628498aae7b24fe266df5356047b86bd
```

**Before and after: Lane on 7th Avenue (2022, new protected lane). Crashes per year on the changed street, and on similar unchanged streets scaled to the same length.**

- Agent's answer: 14.2 → 9.4 a year; similar streets 3.7 → 3.6. Recomputed by the query: matches.
- Source: data/before_after.parquet (crashes_pre, crashes_post, pre_years, post_years, control_pre_rate, control_post_rate), data/edges.parquet (name)

```sql
SELECT sum(b.crashes_pre) AS crashes_before, sum(b.crashes_post) AS crashes_after,
       round(sum(b.crashes_pre) / (sum(b.length_mi * b.pre_years) / sum(b.length_mi)), 1) AS per_year_before,
       round(sum(b.crashes_post) / (sum(b.length_mi * b.post_years) / sum(b.length_mi)), 1) AS per_year_after,
       round(sum(b.control_pre_rate * b.length_mi * b.pre_years) / sum(b.length_mi * b.pre_years) * sum(b.length_mi), 1) AS similar_streets_before,
       round(sum(b.control_post_rate * b.length_mi * b.post_years) / sum(b.length_mi * b.post_years) * sum(b.length_mi), 1) AS similar_streets_after
FROM 'data/before_after.parquet' b JOIN 'data/edges.parquet' e USING (edge_id)
WHERE b.treatment = 'lane: protected' AND b.project LIKE 'bike route%' AND e.name = '7th Avenue'
  AND b.edge_id IN (SELECT edge_id FROM 'docs/sample_routes.csv' WHERE query = 2);
```

## Query 3: I bike from Prospect Heights, Brooklyn to DUMBO, Brooklyn in the evening, around 6pm. Which route should I take?

**Minutes and risk compared with the Fastest route, at the half, assumed, and double rider-share settings**

- Agent's answer: fastest: 15.5 min, 1.0 / 1.0 / 1.0; balanced: 17.7 min, 0.49 / 0.31 / 0.1; lowest_risk: 17.7 min, 0.49 / 0.31 / 0.1. Recomputed by the query: matches.
- Source: docs/sample_routes.csv, data/edges.parquet (travel_min, route_risk_half, route_risk_assumed, route_risk_double)

```sql
SELECT r.route,
       round(sum(e.travel_min), 1) AS minutes,
       round(sum(e.route_risk_half) / max(f.half), 2) AS risk_vs_fastest_half,
       round(sum(e.route_risk_assumed) / max(f.assumed), 2) AS risk_vs_fastest_assumed,
       round(sum(e.route_risk_double) / max(f.double), 2) AS risk_vs_fastest_double
FROM 'docs/sample_routes.csv' r
JOIN 'data/edges.parquet' e USING (edge_id)
CROSS JOIN (SELECT sum(route_risk_half) AS half, sum(route_risk_assumed) AS assumed, sum(route_risk_double) AS double
            FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' USING (edge_id)
            WHERE query = 3 AND route = 'fastest') f
WHERE r.query = 3
GROUP BY r.route ORDER BY minutes;
```

**Safety label 'safest' and recommended route 'balanced'. Rule: 'safest' needs the lowest-risk route's range (risk × (n ± 2 × √n) / n) to sit below every other route's range at all three settings; overlapping ranges mean 'about equally safe', and the faster of the tied routes is recommended. The query shows the assumed setting; swap in route_risk_half and route_risk_double for the other two.**

- Agent's answer: safest.
- Source: docs/sample_routes.csv, data/edges.parquet (route_risk_*, crash_blended_rate, mile_years)

```sql
SELECT route,
       round(sum(e.route_risk_assumed), 2) AS risk,
       round(sum(e.crash_blended_rate * e.mile_years), 1) AS n,
       round(sum(e.route_risk_assumed) * greatest(0, n - 2 * sqrt(n)) / n, 2) AS risk_low,
       round(sum(e.route_risk_assumed) * (n + 2 * sqrt(n)) / n, 2) AS risk_high
FROM 'docs/sample_routes.csv' JOIN 'data/edges.parquet' e USING (edge_id)
WHERE query = 3
GROUP BY route ORDER BY risk;
```

**Stretch 1 that stands out: Flatbush Avenue (segment 42469253-42491599-0). It stands out because range_low is above similar_streets_expected.**

- Agent's answer: 9 crashes (range 3.0 to 15.0), about 0.6 expected on similar streets, main cause Failure to Yield Right-of-Way. Recomputed by the query: matches.
- Source: data/crash_locations.parquet (edge_id), data/crashes.parquet (contributing_factor_vehicle_1), data/edges.parquet (crash_class_rate, mile_years)

```sql
SELECT count(*) AS crashes_since_2021,
       CASE WHEN count(*) < 10 THEN [0.0, 0.0, 0.2, 0.6, 1.1, 1.6, 2.2, 2.8, 3.5, 4.1][count(*) + 1] ELSE round(count(*) - 2 * sqrt(count(*)), 1) END AS range_low,
       CASE WHEN count(*) < 10 THEN [3.7, 5.6, 7.2, 8.8, 10.2, 11.7, 13.1, 14.4, 15.8, 17.1][count(*) + 1] ELSE round(count(*) + 2 * sqrt(count(*)), 1) END AS range_high,
       (SELECT round(crash_class_rate * mile_years, 1) FROM 'data/edges.parquet' WHERE edge_id = '42469253-42491599-0') AS similar_streets_expected,
       (SELECT contributing_factor_vehicle_1 FROM 'data/crash_locations.parquet' l2 JOIN 'data/crashes.parquet' c2 USING (collision_id)
        WHERE l2.edge_id = '42469253-42491599-0' AND contributing_factor_vehicle_1 NOT IN ('Unspecified', 'Does Not Apply')
        GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1) AS main_cause
FROM 'data/crash_locations.parquet' l JOIN 'data/crashes.parquet' c USING (collision_id)
WHERE l.edge_id = '42469253-42491599-0';
```

**One police report behind stretch 1, in the city's own copy of the crash table. Its street names should match Flatbush Avenue.**

- Agent's answer: collision_id b2d3cd65a1168c8740f9f5cb4478931d.
- Source: NYC Open Data, Motor Vehicle Collisions - Crashes (h9gi-nx95)

```sql
-- open in a browser:
-- https://data.cityofnewyork.us/resource/h9gi-nx95.json?collision_id=b2d3cd65a1168c8740f9f5cb4478931d
```
