"""
T051 — Reranker Stability / Batch Calibration
===============================================
Ensures reranker produces stable, calibrated scores across batches.

Problems addressed:
  1. Score drift: Different batch sizes → different score distributions
  2. Position bias: LLM listwise reranking tends to favor early positions
  3. Calibration: Raw scores from LLM are not comparable across calls

Solutions:
  1. Batch normalization: z-score normalize within each batch
  2. Position de-biasing: shuffle input, average multiple runs
  3. Score calibration: map raw scores to [0,1] using sigmoid + temperature
  4. Consistency check: verify reranking is stable across runs
"""
import os
import math
import random
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class CalibratedScore:
    raw_score: float
    calibrated_score: float  # Normalized to [0, 1]
    z_score: float  # Within-batch z-score
    rank: int
    confidence: float  # Based on margin


class BatchCalibrator:
    """Normalizes reranker scores within and across batches."""
    
    def __init__(self, temperature: float = 0.1, min_confidence: float = 0.3):
        self.temperature = temperature
        self.min_confidence = min_confidence
        self.global_stats = {"mean": 0.5, "std": 0.2, "count": 0}
    
    def calibrate_batch(
        self,
        items: List[dict],
        score_field: str = "rerank_score",
        id_field: str = "record_id",
    ) -> List[dict]:
        """Calibrate scores within a batch.
        
        Steps:
        1. Compute batch mean and std
        2. Z-score normalize
        3. Apply sigmoid with temperature for [0,1] mapping
        4. Compute confidence from margin between adjacent ranks
        
        Args:
            items: List of dicts with score_field
            score_field: Field name for raw score
            id_field: Field name for item ID
            
        Returns:
            Items with added fields: calibrated_score, z_score, confidence
        """
        if not items:
            return items
        
        scores = [item.get(score_field, 0.5) for item in items]
        
        # Compute batch stats
        batch_mean = sum(scores) / len(scores)
        batch_std = max(math.sqrt(sum((s - batch_mean) ** 2 for s in scores) / len(scores)), 0.01)
        
        # Z-score normalize and calibrate
        calibrated_items = []
        for i, (item, score) in enumerate(zip(items, scores)):
            z = (score - batch_mean) / batch_std
            
            # Sigmoid calibration: maps z-score to [0, 1]
            calibrated = 1.0 / (1.0 + math.exp(-z / self.temperature))
            
            # Update item
            item_copy = dict(item)
            item_copy["calibrated_score"] = calibrated
            item_copy["z_score"] = z
            item_copy["batch_mean"] = batch_mean
            item_copy["batch_std"] = batch_std
            
            calibrated_items.append(item_copy)
        
        # Sort by calibrated score (descending)
        calibrated_items.sort(key=lambda x: x["calibrated_score"], reverse=True)
        
        # Assign ranks and compute confidence from margin
        for rank, item in enumerate(calibrated_items, 1):
            item["rank"] = rank
            
            # Confidence based on margin to next item
            if rank < len(calibrated_items):
                margin = item["calibrated_score"] - calibrated_items[rank]["calibrated_score"]
            else:
                margin = item["calibrated_score"]  # Last item confidence = its score
            
            # Normalize margin to [0, 1]
            item["confidence"] = max(self.min_confidence, min(1.0, margin * 5))
        
        # Update global stats for cross-batch normalization
        self._update_global_stats(batch_mean, batch_std, len(items))
        
        return calibrated_items
    
    def _update_global_stats(self, batch_mean: float, batch_std: float, count: int):
        """Update running global statistics for cross-batch calibration."""
        total_count = self.global_stats["count"] + count
        if total_count == 0:
            return
        
        # Exponential moving average
        alpha = count / total_count
        self.global_stats["mean"] = (1 - alpha) * self.global_stats["mean"] + alpha * batch_mean
        self.global_stats["std"] = (1 - alpha) * self.global_stats["std"] + alpha * batch_std
        self.global_stats["count"] = total_count
    
    def get_global_stats(self) -> dict:
        return dict(self.global_stats)


def de_bias_positions(
    items: List[dict],
    rerank_fn,
    n_shuffles: int = 3,
    id_field: str = "record_id",
) -> List[dict]:
    """Reduce position bias by averaging multiple shuffled rerankings.
    
    Args:
        items: Input items to rerank
        rerank_fn: Function that takes items and returns reranked items
        n_shuffles: Number of shuffled runs to average
        id_field: Field name for item ID
        
    Returns:
        De-biased reranked items
    """
    if len(items) <= 1 or n_shuffles <= 0:
        return rerank_fn(items) if callable(rerank_fn) else items
    
    # Collect scores across runs
    score_accum: Dict[int, List[float]] = {item[id_field]: [] for item in items}
    
    for run_idx in range(n_shuffles):
        # Shuffle input order
        shuffled = list(items)
        random.seed(42 + run_idx)  # Reproducible shuffles
        random.shuffle(shuffled)
        
        # Rerank
        reranked = rerank_fn(shuffled)
        
        # Record positions (inverted: higher = better)
        n = len(reranked)
        for rank, item in enumerate(reranked, 1):
            inverted_score = (n - rank + 1) / n  # 1.0 for rank 1, decreasing
            score_accum[item[id_field]].append(inverted_score)
    
    # Average scores across runs
    avg_scores = {}
    for rid, scores in score_accum.items():
        avg_scores[rid] = sum(scores) / len(scores)
    
    # Sort by average score
    result = sorted(items, key=lambda x: avg_scores.get(x[id_field], 0), reverse=True)
    
    # Add stability metadata
    for rank, item in enumerate(result, 1):
        rid = item[id_field]
        scores = score_accum[rid]
        mean_score = sum(scores) / len(scores)
        std = math.sqrt(sum((s - mean_score) ** 2 for s in scores) / max(len(scores), 1))
        item["stability_score"] = mean_score
        item["stability_std"] = std
        item["rank"] = rank
    
    return result


def check_reranker_stability(
    items: List[dict],
    rerank_fn,
    n_runs: int = 3,
    id_field: str = "record_id",
) -> dict:
    """Check if reranker produces stable results across multiple runs.
    
    Metrics:
    - Kendall's tau between runs (rank correlation)
    - Top-k overlap (how many items are in top-k across runs)
    - Score variance
    
    Returns:
        {
            "stable": bool,
            "kendall_tau": float,
            "top_k_overlap": float,
            "score_cv": float (coefficient of variation),
        }
    """
    if len(items) <= 1 or n_runs <= 1:
        return {"stable": True, "kendall_tau": 1.0, "top_k_overlap": 1.0, "score_cv": 0.0}
    
    # Run reranker multiple times
    all_runs = []
    for i in range(n_runs):
        reranked = rerank_fn(list(items))
        all_runs.append([item[id_field] for item in reranked])
    
    # Compute pairwise Kendall's tau
    taus = []
    for i in range(len(all_runs)):
        for j in range(i + 1, len(all_runs)):
            tau = _kendall_tau(all_runs[i], all_runs[j])
            taus.append(tau)
    
    avg_tau = sum(taus) / max(len(taus), 1)
    
    # Compute top-k overlap (k = min(5, len/2))
    k = min(5, len(items) // 2)
    if k > 0:
        top_k_sets = [set(run[:k]) for run in all_runs]
        intersection = set.intersection(*top_k_sets) if top_k_sets else set()
        union = set.union(*top_k_sets) if top_k_sets else set()
        overlap = len(intersection) / max(len(union), 1)
    else:
        overlap = 1.0
    
    # Score coefficient of variation across runs
    all_scores: Dict[int, List[int]] = {}
    for run in all_runs:
        for rank, rid in enumerate(run, 1):
            all_scores.setdefault(rid, []).append(rank)
    
    cvs = []
    for rid, ranks in all_scores.items():
        mean_rank = sum(ranks) / len(ranks)
        std_rank = math.sqrt(sum((r - mean_rank) ** 2 for r in ranks) / max(len(ranks), 1))
        if mean_rank > 0:
            cvs.append(std_rank / mean_rank)
    
    avg_cv = sum(cvs) / max(len(cvs), 1) if cvs else 0.0
    
    return {
        "stable": avg_tau >= 0.7 and overlap >= 0.6,
        "kendall_tau": round(avg_tau, 3),
        "top_k_overlap": round(overlap, 3),
        "score_cv": round(avg_cv, 3),
    }


def _kendall_tau(rank1: List[int], rank2: List[int]) -> float:
    """Compute Kendall's tau rank correlation between two rankings."""
    if len(rank1) != len(rank2) or len(rank1) < 2:
        return 1.0
    
    n = len(rank1)
    concordant = 0
    discordant = 0
    
    # Create position maps
    pos1 = {rid: i for i, rid in enumerate(rank1)}
    pos2 = {rid: i for i, rid in enumerate(rank2)}
    
    common = set(pos1.keys()) & set(pos2.keys())
    common_list = list(common)
    
    for i in range(len(common_list)):
        for j in range(i + 1, len(common_list)):
            a, b = common_list[i], common_list[j]
            # Check concordance
            if (pos1[a] - pos1[b]) * (pos2[a] - pos2[b]) > 0:
                concordant += 1
            else:
                discordant += 1
    
    total = concordant + discordant
    if total == 0:
        return 1.0
    
    return (concordant - discordant) / total
