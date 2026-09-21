# Cross-tenant tier log

One artifact, three runs.

| deployment | step | rule | tier | |
|---|---|---|---|---|
| base | s2 | label_proximity | 0 |  |
| base | s3 | role_name | 0 |  |
| base | s4 | label_proximity | 0 |  |
| base | s5 | row_cell | 0 |  |
| base | -> | Success | 4210.33 |  |
| riverbend (no override) | s2 | region_path | 1 | **degraded** |
| riverbend (no override) | s3 | role_name | 0 |  |
| riverbend (no override) | s4 | label_proximity | 0 |  |
| riverbend (no override) | s5 | row_cell | 0 |  |
| riverbend (no override) | -> | Success | 4210.33 |  |
| riverbend + override | s2 | label_proximity | 0 |  |
| riverbend + override | s3 | role_name | 0 |  |
| riverbend + override | s4 | label_proximity | 0 |  |
| riverbend + override | s5 | row_cell | 0 |  |
| riverbend + override | -> | Success | 4210.33 |  |
