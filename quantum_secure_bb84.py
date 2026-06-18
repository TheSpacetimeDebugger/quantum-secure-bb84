"""
quantum_secure_bb84.py
======================
BB84 Quantum Key Distribution Protocol
Author : Ibrahim El-Shami — Quantum Software Developer & Emerging Tech Entrepreneur
License: MIT
Requires: qiskit>=1.0.0, numpy>=1.26.0

Protocol stages implemented
----------------------------
1. Bit Generation          — Alice generates random classical bits.
2. Basis Selection         — Alice & Bob independently pick Rectilinear (+) or Diagonal (x).
3. Quantum State Prep      — Qiskit QuantumCircuit encodes each qubit.
4. Eavesdropping (Eve)     — Probabilistic intercept-resend attack via NumPy.
5. Transmission & Measure  — Bob measures in his chosen basis.
6. Basis Reconciliation    — Public channel comparison; mismatched bases discarded.
7. Key Sifting             — Retained bits form the raw sifted key.
8. QBER Estimation         — Sample subset; flag compromise if QBER > 11 %.
"""

from __future__ import annotations

import sys
import textwrap
from typing import List, Tuple

import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

QBER_THRESHOLD: float = 0.11          # Heisenberg distortion boundary (11 %)
QBER_SAMPLE_FRACTION: float = 0.25    # Fraction of sifted key used for QBER check
MIN_KEY_BITS: int = 8                  # Minimum usable key length after sifting
RECTILINEAR: int = 0                  # Basis symbol: |0⟩/|1⟩  (+)
DIAGONAL: int = 1                     # Basis symbol: |+⟩/|−⟩  (×)

# ──────────────────────────────────────────────────────────────────────────────
# Colour helpers (ANSI — gracefully degraded on Windows without VT support)
# ──────────────────────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _green(t: str)  -> str: return _c("32;1", t)
def _red(t: str)    -> str: return _c("31;1", t)
def _cyan(t: str)   -> str: return _c("36;1", t)
def _yellow(t: str) -> str: return _c("33;1", t)
def _bold(t: str)   -> str: return _c("1",    t)
def _dim(t: str)    -> str: return _c("2",    t)

# ──────────────────────────────────────────────────────────────────────────────
# Step 1 — Random bit & basis generation
# ──────────────────────────────────────────────────────────────────────────────

def generate_random_bits(n: int, rng: np.random.Generator) -> np.ndarray:
    """Return *n* uniformly random bits {0, 1}."""
    return rng.integers(0, 2, size=n, dtype=np.uint8)

def generate_random_bases(n: int, rng: np.random.Generator) -> np.ndarray:
    """Return *n* bases: 0 = Rectilinear (+), 1 = Diagonal (×)."""
    return rng.integers(0, 2, size=n, dtype=np.uint8)

# ──────────────────────────────────────────────────────────────────────────────
# Step 2 — Quantum circuit encoding (Qiskit 1.0+ compatible)
# ──────────────────────────────────────────────────────────────────────────────

def encode_qubits(
    bits: np.ndarray,
    bases: np.ndarray,
) -> List[QuantumCircuit]:
    """
    Encode each classical bit into a qubit according to the chosen basis.

    Rectilinear (+):
        bit=0  →  |0⟩  (no gate)
        bit=1  →  |1⟩  (X gate)
    Diagonal (×):
        bit=0  →  |+⟩  (H gate)
        bit=1  →  |−⟩  (X then H gate)
    """
    circuits: List[QuantumCircuit] = []
    for bit, basis in zip(bits, bases):
        qc = QuantumCircuit(1, 1)
        if bit == 1:
            qc.x(0)                 # Flip |0⟩ → |1⟩
        if basis == DIAGONAL:
            qc.h(0)                 # Rotate to diagonal basis |±⟩
        circuits.append(qc)
    return circuits

# ──────────────────────────────────────────────────────────────────────────────
# Step 3 — Eve: intercept-resend attack
# ──────────────────────────────────────────────────────────────────────────────

def eve_intercept(
    circuits: List[QuantumCircuit],
    intercept_probability: float,
    rng: np.random.Generator,
    simulator: AerSimulator,
) -> Tuple[List[QuantumCircuit], int]:
    """
    Probabilistic eavesdropper.

    Eve intercepts each qubit independently with *intercept_probability*.
    She measures in a randomly chosen basis, then re-encodes and forwards the
    (potentially disturbed) qubit to Bob. This mirrors the physical no-cloning
    constraint: Eve must collapse the superposition to observe it.

    Returns
    -------
    disturbed_circuits : List[QuantumCircuit]
        Circuits forwarded to Bob (may be state-collapsed by Eve).
    intercepted_count : int
        How many qubits Eve actually touched.
    """
    intercepted_count = 0
    disturbed: List[QuantumCircuit] = []

    for qc in circuits:
        if rng.random() < intercept_probability:
            intercepted_count += 1
            eve_basis = int(rng.integers(0, 2))  # 0=rectilinear, 1=diagonal

            # Build measurement circuit
            measure_qc = qc.copy()
            if eve_basis == DIAGONAL:
                measure_qc.h(0)
            measure_qc.measure(0, 0)

            # Execute on noise-free simulator
            compiled = transpile(measure_qc, simulator)
            job = simulator.run(compiled, shots=1)
            counts = job.result().get_counts()
            eve_bit = int(list(counts.keys())[0])

            # Re-encode with Eve's (possibly wrong) measurement result
            new_qc = QuantumCircuit(1, 1)
            if eve_bit == 1:
                new_qc.x(0)
            if eve_basis == DIAGONAL:
                new_qc.h(0)
            disturbed.append(new_qc)
        else:
            disturbed.append(qc)

    return disturbed, intercepted_count

# ──────────────────────────────────────────────────────────────────────────────
# Step 4 — Bob measures incoming qubits
# ──────────────────────────────────────────────────────────────────────────────

def bob_measure(
    circuits: List[QuantumCircuit],
    bob_bases: np.ndarray,
    simulator: AerSimulator,
) -> np.ndarray:
    """
    Bob measures each qubit in his chosen basis.

    Rectilinear (+): measure directly in computational basis.
    Diagonal (×):    apply H before measuring.
    """
    bob_bits: List[int] = []
    for qc, basis in zip(circuits, bob_bases):
        measure_qc = qc.copy()
        if basis == DIAGONAL:
            measure_qc.h(0)
        measure_qc.measure(0, 0)

        compiled = transpile(measure_qc, simulator)
        job = simulator.run(compiled, shots=1)
        counts = job.result().get_counts()
        bob_bits.append(int(list(counts.keys())[0]))

    return np.array(bob_bits, dtype=np.uint8)

# ──────────────────────────────────────────────────────────────────────────────
# Step 5 — Basis reconciliation & key sifting
# ──────────────────────────────────────────────────────────────────────────────

def sift_key(
    alice_bits: np.ndarray,
    alice_bases: np.ndarray,
    bob_bits: np.ndarray,
    bob_bases: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Discard positions where Alice and Bob chose different bases.

    Returns
    -------
    alice_sifted : np.ndarray   Alice's retained bits
    bob_sifted   : np.ndarray   Bob's retained bits
    match_mask   : np.ndarray   Boolean mask of matching-basis positions
    """
    match_mask = alice_bases == bob_bases
    return alice_bits[match_mask], bob_bits[match_mask], match_mask

# ──────────────────────────────────────────────────────────────────────────────
# Step 6 — QBER estimation (Heisenberg distortion detection)
# ──────────────────────────────────────────────────────────────────────────────

def estimate_qber(
    alice_sifted: np.ndarray,
    bob_sifted: np.ndarray,
    sample_fraction: float,
    rng: np.random.Generator,
) -> Tuple[float, np.ndarray, np.ndarray]:
    """
    Sample *sample_fraction* of the sifted key publicly to estimate QBER.

    Returns
    -------
    qber          : float      Quantum Bit Error Rate in [0, 1]
    alice_final   : np.ndarray Remaining key bits (sample positions discarded)
    bob_final     : np.ndarray Remaining key bits (Bob's side)
    """
    n = len(alice_sifted)
    sample_size = max(1, int(n * sample_fraction))
    sample_idx = rng.choice(n, size=sample_size, replace=False)
    errors = np.sum(alice_sifted[sample_idx] != bob_sifted[sample_idx])
    qber = errors / sample_size

    # Remove sample positions from final key
    keep_mask = np.ones(n, dtype=bool)
    keep_mask[sample_idx] = False
    return float(qber), alice_sifted[keep_mask], bob_sifted[keep_mask]

# ──────────────────────────────────────────────────────────────────────────────
# Pretty-print utilities
# ──────────────────────────────────────────────────────────────────────────────

def _basis_str(bases: np.ndarray) -> str:
    return " ".join("+" if b == RECTILINEAR else "×" for b in bases)

def _bits_str(bits: np.ndarray) -> str:
    return " ".join(str(int(b)) for b in bits)

def _match_str(alice_bases: np.ndarray, bob_bases: np.ndarray) -> str:
    tokens = []
    for a, b in zip(alice_bases, bob_bases):
        if a == b:
            tokens.append(_green("✓"))
        else:
            tokens.append(_red("✗"))
    return " ".join(tokens)

def _header(title: str) -> None:
    width = 72
    bar = "═" * width
    print(f"\n{_cyan(bar)}")
    print(_bold(f"  {title}"))
    print(_cyan(bar))

# ──────────────────────────────────────────────────────────────────────────────
# Main protocol runner
# ──────────────────────────────────────────────────────────────────────────────

def run_bb84(
    n_qubits: int = 64,
    eve_intercept_prob: float = 0.0,
    seed: int | None = None,
) -> dict:
    """
    Execute the full BB84 QKD protocol.

    Parameters
    ----------
    n_qubits            : int    Number of qubits Alice transmits.
    eve_intercept_prob  : float  Probability Eve intercepts each qubit [0, 1].
    seed                : int    RNG seed for reproducibility.

    Returns
    -------
    result : dict
        {
            "secure"       : bool,
            "qber"         : float,
            "sifted_key"   : np.ndarray,
            "final_key"    : np.ndarray,
            "intercepted"  : int,
        }
    """
    rng = np.random.default_rng(seed)
    simulator = AerSimulator()

    # ── Stage 1: Alice generates bits and bases ────────────────────────────
    _header("STAGE 1 — Alice: Random Bit & Basis Generation")
    alice_bits  = generate_random_bits(n_qubits, rng)
    alice_bases = generate_random_bases(n_qubits, rng)

    print(f"  {'Alice bits  :':<20} {_bits_str(alice_bits)}")
    print(f"  {'Alice bases :':<20} {_basis_str(alice_bases)}")

    # ── Stage 2: Encode qubits ─────────────────────────────────────────────
    _header("STAGE 2 — Quantum State Preparation (Qiskit Encoding)")
    circuits = encode_qubits(alice_bits, alice_bases)
    print(f"  {_bold(str(n_qubits))} qubits encoded.")
    print(f"  Rectilinear (+): {{0}}→|0⟩  {{1}}→|1⟩")
    print(f"  Diagonal     (×): {{0}}→|+⟩  {{1}}→|−⟩")

    # ── Stage 3: Eve intercept-resend ──────────────────────────────────────
    _header("STAGE 3 — Quantum Channel / Eve Intercept-Resend Attack")
    circuits, intercepted_count = eve_intercept(
        circuits, eve_intercept_prob, rng, simulator
    )
    if eve_intercept_prob > 0:
        print(f"  {_yellow('⚠  Eve active')} — intercept probability : "
              f"{_bold(f'{eve_intercept_prob:.0%}')}")
        print(f"  Qubits intercepted by Eve : {_bold(str(intercepted_count))}")
    else:
        print(f"  {_green('✓  Channel clean')} — no eavesdropper active.")

    # ── Stage 4: Bob chooses bases and measures ────────────────────────────
    _header("STAGE 4 — Bob: Basis Selection & Quantum Measurement")
    bob_bases = generate_random_bases(n_qubits, rng)
    bob_bits  = bob_measure(circuits, bob_bases, simulator)

    print(f"  {'Bob bases   :':<20} {_basis_str(bob_bases)}")
    print(f"  {'Bob bits    :':<20} {_bits_str(bob_bits)}")

    # ── Stage 5: Basis reconciliation ─────────────────────────────────────
    _header("STAGE 5 — Public Basis Reconciliation")
    alice_sifted, bob_sifted, match_mask = sift_key(
        alice_bits, alice_bases, bob_bits, bob_bases
    )
    n_match = int(match_mask.sum())

    print(f"  Basis match map : {_match_str(alice_bases, bob_bases)}")
    print(f"  Matching bases  : {_bold(str(n_match))} / {n_qubits}  "
          f"({_dim('mismatched positions discarded')})")
    print(f"\n  {'Alice sifted key :':<22} {_bits_str(alice_sifted)}")
    print(f"  {'Bob   sifted key :':<22} {_bits_str(bob_sifted)}")

    if n_match < MIN_KEY_BITS:
        print(_red(f"\n  ✗  Insufficient sifted bits ({n_match} < {MIN_KEY_BITS}). "
                   f"Retry with more qubits."))
        return {
            "secure": False, "qber": None,
            "sifted_key": alice_sifted, "final_key": np.array([]),
            "intercepted": intercepted_count,
        }

    # ── Stage 6: QBER estimation ───────────────────────────────────────────
    _header("STAGE 6 — QBER Estimation: Heisenberg Distortion Detection")
    qber, alice_final, bob_final = estimate_qber(
        alice_sifted, bob_sifted, QBER_SAMPLE_FRACTION, rng
    )

    print(f"  Sample fraction : {QBER_SAMPLE_FRACTION:.0%} of sifted key")
    print(f"  QBER measured   : {_bold(f'{qber:.4f}')}  "
          f"({_bold(f'{qber:.2%}')})")
    print(f"  QBER threshold  : {QBER_THRESHOLD:.0%}  "
          f"(Heisenberg uncertainty boundary)")

    # ── Stage 7: Security verdict ──────────────────────────────────────────
    _header("STAGE 7 — Cryptographic Security Verdict")
    secure = qber <= QBER_THRESHOLD

    if secure:
        key_hex = "".join(str(int(b)) for b in alice_final)
        key_int = int(key_hex, 2) if key_hex else 0
        print(f"  {_green('✓  SECURE KEY ESTABLISHED')}")
        print(f"\n  Final key length  : {_bold(str(len(alice_final)))} bits")
        print(f"  Final key (binary): {_green(_bits_str(alice_final))}")
        print(f"  Final key (hex)   : {_green(hex(key_int))}")
        if np.any(alice_final != bob_final):
            print(f"  {_yellow('⚠  Note: residual bit mismatches present — '
                               'apply privacy amplification in production.')}")
        else:
            print(f"  {_green('✓  Alice ≡ Bob on final key — perfect correlation.')}")
    else:
        print(f"  {_red('✗  EAVESDROPPER DETECTED — KEY ABORTED')}")
        print(f"\n  QBER {qber:.2%} exceeds threshold {QBER_THRESHOLD:.0%}.")
        print(f"  The Heisenberg Uncertainty Principle prohibits Eve from")
        print(f"  measuring quantum states without inducing detectable disturbance.")
        print(f"  All key material has been discarded. Initiate re-keying.")

    print(f"\n{_cyan('═' * 72)}\n")

    return {
        "secure"     : secure,
        "qber"       : qber,
        "sifted_key" : alice_sifted,
        "final_key"  : alice_final if secure else np.array([]),
        "intercepted": intercepted_count,
    }

# ──────────────────────────────────────────────────────────────────────────────
# Entry point — two demo scenarios
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    BANNER = textwrap.dedent("""
    ╔══════════════════════════════════════════════════════════════════════╗
    ║   BB84 Quantum Key Distribution — Heisenberg Distortion Detection   ║
    ║   Qiskit 1.0+ · NumPy · AerSimulator                                ║
    ║   Author: Ibrahim El-Shami — Quantum Software Developer             ║
    ╚══════════════════════════════════════════════════════════════════════╝
    """)
    print(_cyan(BANNER))

    # ── Scenario A: No eavesdropper ────────────────────────────────────────
    print(_bold("━━━  SCENARIO A: Clean Channel (No Eavesdropper)  ━━━"))
    result_a = run_bb84(n_qubits=64, eve_intercept_prob=0.0, seed=42)

    # ── Scenario B: Active Eve (50 % intercept rate) ───────────────────────
    print(_bold("━━━  SCENARIO B: Active Eavesdropper (50 % intercept rate)  ━━━"))
    result_b = run_bb84(n_qubits=64, eve_intercept_prob=0.50, seed=42)
