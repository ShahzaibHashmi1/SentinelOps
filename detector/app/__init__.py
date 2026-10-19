"""SentinelOps detector (module M4, tier 1): threshold rules plus incident correlation.

Standard library only. Layout (CP1 builds the first five modules, CP2 the rest):
  model.py       data classes: Observation, Symptom, Incident, DataGap; the Detector interface
  rules.py       rules.json loader and validator (margin rule included)
  sources.py     the interface of a data source (Prometheus or Loki); HTTP clients come in CP2
  thresholds.py  ThresholdDetector: rules + sources -> observations
  correlator.py  observations -> symptoms -> incidents (pure logic, no I/O)
  store.py       SQLite store: one writer, read-only readers
"""
