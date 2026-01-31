"""
Inference module for optimized PyTorch inference on AMD GPU
"""
from .batch_engine import BatchInferenceEngine
from .model_manager import ModelManager

__all__ = ['ModelManager', 'BatchInferenceEngine']
