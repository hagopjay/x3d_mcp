# Evidence states for the nine cases

Written by `build/evidence.py`. Each claim has one state; see the script's docstring for what they mean.

| State | Claims |
|---|---|
| computed here | 162 |
| explained difference | 13 |
| hash checked | 1 |
| not packaged | 2 |
| recorded | 60 |

| Case | computed here | explained difference | hash checked | not packaged | recorded |
|---|---|---|---|---|---|
| heritage | 18 | 3 | 0 | 0 | 5 |
| factory | 18 | 0 | 0 | 0 | 8 |
| hospital | 18 | 0 | 0 | 0 | 8 |
| building | 18 | 0 | 0 | 0 | 8 |
| warehouse | 18 | 0 | 0 | 0 | 8 |
| city | 18 | 0 | 0 | 0 | 8 |
| concert | 18 | 4 | 0 | 0 | 4 |
| firecode | 18 | 6 | 0 | 0 | 2 |
| farm | 18 | 0 | 0 | 0 | 8 |

## Claims that are not plain matches

- **hash checked**: 75 packaged files match the manifest (`files`). 75 files, sha256 each
- **explained difference**: Recorded MCP run of H1 (clean) (`heritage/clean/H1/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of H3 (clean) (`heritage/clean/H3/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of H3 (fault) (`heritage/fault/H3/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of M1 (clean) (`concert/clean/M1/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of INF (clean) (`concert/clean/INF/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of M1 (fault) (`concert/fault/M1/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of INF (fault) (`concert/fault/INF/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of E1 (clean) (`firecode/clean/E1/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of E2 (clean) (`firecode/clean/E2/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of INF (clean) (`firecode/clean/INF/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of E1 (fault) (`firecode/fault/E1/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of E2 (fault) (`firecode/fault/E2/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **explained difference**: Recorded MCP run of INF (fault) (`firecode/fault/INF/recorded`). same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY (Evidence Edition F03); today's server keeps it
- **not packaged**: N3.js, Comunica and shacl-engine match the oracle on 90 of 90 runs (`browser`). measured on the x3d.demo page (demos/semantic-twins/v1); the browser engines are not in this folder
- **not packaged**: All 116 class and property uses are declared in each publisher's vocabulary file (`vocab`). checked against the publishers' files as read on 16 Nov 2026; those files are not kept here
