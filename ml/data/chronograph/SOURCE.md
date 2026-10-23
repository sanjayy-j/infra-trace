# ChronoGraph dependency graph (third-party data)

`edges.csv` is the service dependency graph from **ChronoGraph**, redistributed
here under its Apache-2.0 licence for reproducibility of the topology
experiments in `docs/ml/topology-study.md`.

- **Source:** https://github.com/bit-ml/ChronoGraph (`dataset/edges.csv`)
- **Paper:** Lutu, Pintilie, Burceanu & Manolache, *ChronoGraph: A Real-World
  Graph-Based Multivariate Time Series Dataset*, arXiv:2509.04449
- **Licence:** Apache-2.0
- **Contents:** 708 services, 1529 directed dependency edges, from
  Bitdefender production microservices.
- **What is NOT included:** the node/edge metric time series and the
  expert-annotated incident labels. Those live in Git-LFS objects of ~1.8 GB
  and are not needed here - this study uses the TOPOLOGY only.

An edge `source,target` means *source depends on target*, so failure
propagates target -> source. `ml/realtopo.py` performs that reversal exactly
once, in the same place and the same way as `ml/graph.from_edges`.

## What this data is and is not used for

Used: real dependency structure, to test whether conclusions drawn on
synthetic topology survive on a real one.

NOT used: as real-world validation of failure propagation. There are no
propagation labels here. Cascades over this topology are still SIMULATED by
`ml/synth.simulate_cascade`, and every result on it is a synthetic-process
result on real structure. The only real propagation evidence in this project
remains the 22 InfraTrace incidents.
