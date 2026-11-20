"""Multi-agent News & Insights Aggregator."""

import os
import sys

__version__ = "0.1.0"

# Native-library guard rails. These must run before torch / faiss / scikit-learn are imported,
# which is why they live in the package __init__.
#
# On macOS the faiss-cpu, torch and scikit-learn wheels each bundle their own copy of the
# OpenMP runtime (libomp.dylib). With more than one copy active in the process, the first
# multi-threaded call (SentenceTransformer.encode or a FAISS search) segfaults. Keeping
# OpenMP single-threaded avoids it, and costs nothing at a few hundred articles per run.
# setdefault() means anything exported in the shell still wins.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
if sys.platform == "darwin":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
