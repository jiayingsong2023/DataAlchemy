import torch

def test_gpu():
    print(f"PyTorch version: {torch.__version__}")
    
    cuda_available = torch.cuda.is_available()
    print(f"Is ROCm/CUDA available? {cuda_available}")
    
    if cuda_available:
        print(f"Device name: {torch.cuda.get_device_name(0)}")
        print(f"Device count: {torch.cuda.device_count()}")
        
        # Simple tensor operation on GPU
        x = torch.tensor([1.0, 2.0, 3.0]).to("cuda")
        y = x * 2
        print(f"Result of tensor operation on GPU: {y}")
        print("GPU test successful!")
    else:
        print("GPU not detected. Please check your ROCm installation and drivers.")

if __name__ == "__main__":
    test_gpu()
