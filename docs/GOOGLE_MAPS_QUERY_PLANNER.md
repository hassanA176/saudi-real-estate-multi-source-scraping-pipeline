# Google Maps Query Planner

The current public configuration contains:

- **13 Saudi administrative regions**
- **135 configured city/locality names**
- **148 geographic targets** after adding region-level searches
- 4 requested categories: chalet, camp, studio, apartment
- daily/monthly search variants for studio and apartment

The current configuration produces **840 unique search queries per complete sweep** after query deduplication.

The runner starts with Riyadh and Jeddah, then covers the remaining configured localities and regional searches. The query planner removes duplicate search strings before execution.

The collector does not claim that Google Maps itself exposes every rental property in Saudi Arabia. Coverage depends on what the public Google Maps UI returns for each query.
