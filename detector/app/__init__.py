"""SentinelOps detector (module M4, tier 1): threshold rules plus incident correlation.

Standard library only. Modules:
  model.py          data classes: Observation, Symptom, Incident, DataGap; the Detector interface
  rules.py          rules.json loader and validator (margin rule included)
  sources.py        the Source interface and the HTTP clients for Prometheus and Loki (warm-up, no_data)
  thresholds.py     ThresholdDetector: rules + sources -> observations
  correlator.py     observations -> symptoms -> incidents (pure logic, no I/O)
  store.py          SQLite store: one writer, read-only readers
  loop.py           the evaluation loop (one thread, one cycle every EVAL_INTERVAL_S)
  api.py            read-only HTTP API (GET only) on port 9100
  metrics.py        /metrics in the Prometheus text format
  logging_setup.py  JSON log lines with the field names of the M1 services
  main.py           settings from environment variables, start order, signals
"""
