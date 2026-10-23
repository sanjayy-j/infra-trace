"""
InfraTrace ML layer - probabilistic blast-radius refinement.

The database remains the system of record. This package adds a predictive
layer on top of it and never replaces a SQL result with a model output:
the API reports reachability (observed structure, computed by the recursive
CTE) and the predicted probability (model) as separate fields.
"""
