#!/usr/bin/env python3
"""
GPU Detection and Testing Script
Supports Ubuntu/Linux with ROCm/CUDA
"""
import sys
import platform
import subprocess

def check_system_info():
    """Display system information"""
    print("=" * 60)
    print("System Information")
    print("=" * 60)
    print(f"Platform: {platform.system()}")
    print(f"Platform Release: {platform.release()}")
    print(f"Architecture: {platform.machine()}")
    print(f"Python Version: {sys.version}")
    print()

def check_rocm_runtime():
    """Check if ROCm runtime is available (Linux)"""
    print("=" * 60)
    print("ROCm Runtime Check (Linux)")
    print("=" * 60)
    
    try:
        # Check rocm-smi
        result = subprocess.run(['rocm-smi'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✅ rocm-smi found:")
            print(result.stdout)
            return True
        else:
            print("⚠️  rocm-smi returned error:")
            print(result.stderr)
            return False
    except FileNotFoundError:
        print("❌ rocm-smi not found. ROCm may not be installed.")
        print("   Install ROCm: https://rocm.docs.amd.com/")
        return False
    except subprocess.TimeoutExpired:
        print("⚠️  rocm-smi timeout")
        return False
    except Exception as e:
        print(f"⚠️  Error checking ROCm: {e}")
        return False

def check_nvidia_runtime():
    """Check if NVIDIA CUDA runtime is available"""
    print("=" * 60)
    print("NVIDIA CUDA Runtime Check")
    print("=" * 60)
    
    try:
        result = subprocess.run(['nvidia-smi'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✅ nvidia-smi found:")
            print(result.stdout)
            return True
        else:
            print("⚠️  nvidia-smi returned error")
            return False
    except FileNotFoundError:
        print("ℹ️  nvidia-smi not found (not an NVIDIA GPU)")
        return False
    except Exception as e:
        print(f"⚠️  Error checking NVIDIA: {e}")
        return False

def test_pytorch_gpu():
    """Test PyTorch GPU availability"""
    print("=" * 60)
    print("PyTorch GPU Detection")
    print("=" * 60)
    
    try:
        import torch
        print(f"✅ PyTorch version: {torch.__version__}")
    
        # Check CUDA/ROCm availability
    cuda_available = torch.cuda.is_available()
        print(f"GPU Available (torch.cuda.is_available()): {cuda_available}")
    
    if cuda_available:
            device_count = torch.cuda.device_count()
            print(f"✅ GPU Device Count: {device_count}")
            
            for i in range(device_count):
                print(f"\n--- GPU {i} ---")
                print(f"  Name: {torch.cuda.get_device_name(i)}")
                print(f"  Capability: {torch.cuda.get_device_capability(i)}")
                
                # Get memory info
                props = torch.cuda.get_device_properties(i)
                print(f"  Total Memory: {props.total_memory / 1024**3:.2f} GB")
                print(f"  Multiprocessors: {props.multi_processor_count}")
            
            # Test tensor operation
            print("\n--- Testing GPU Computation ---")
            try:
        x = torch.tensor([1.0, 2.0, 3.0]).to("cuda")
        y = x * 2
                print(f"✅ GPU computation successful!")
                print(f"   Result: {y.cpu().numpy()}")
                
                # Test matrix multiplication
                a = torch.randn(100, 100).to("cuda")
                b = torch.randn(100, 100).to("cuda")
                c = torch.matmul(a, b)
                print(f"✅ GPU matrix multiplication successful!")
                print(f"   Result shape: {c.shape}")
                
                return True
            except Exception as e:
                print(f"❌ GPU computation failed: {e}")
                return False
        else:
            print("❌ No GPU detected by PyTorch")
            print("\nTroubleshooting:")
            print("1. Check if GPU drivers are installed")
            print("2. For AMD: Install ROCm (https://rocm.docs.amd.com/)")
            print("3. For NVIDIA: Install CUDA toolkit")
            print("4. Verify PyTorch was installed with GPU support")
            return False
            
    except ImportError:
        print("❌ PyTorch not installed")
        print("   Install with: uv sync")
        return False
    except Exception as e:
        print(f"❌ Error testing PyTorch GPU: {e}")
        import traceback
        traceback.print_exc()
        return False

def check_backend():
    """Check PyTorch backend information"""
    print("=" * 60)
    print("PyTorch Backend Information")
    print("=" * 60)
    
    try:
        import torch
        print(f"PyTorch Version: {torch.__version__}")
        print(f"CUDA Available: {torch.cuda.is_available()}")
        
        if torch.cuda.is_available():
            print(f"CUDA Version: {torch.version.cuda}")
            print(f"cuDNN Version: {torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else 'N/A'}")
        
        # Check if compiled with ROCm
        if hasattr(torch.version, 'hip'):
            print(f"ROCm/HIP Version: {torch.version.hip}")
        
        # Check device type
        if torch.cuda.is_available():
            print(f"Device Type: {torch.cuda.get_device_name(0)}")
    except Exception as e:
        print(f"Error checking backend: {e}")

def main():
    """Main GPU detection and testing function"""
    print("\n" + "=" * 60)
    print("GPU Detection and Testing Tool")
    print("=" * 60)
    print()
    
    # System info
    check_system_info()
    
    # Platform-specific checks
    is_linux = platform.system() == "Linux"
    
    if is_linux:
        print("Detected Linux platform - checking ROCm and NVIDIA...")
        rocm_ok = check_rocm_runtime()
        nvidia_ok = check_nvidia_runtime()
        print()
    else:
        print(f"⚠️  Warning: This script is optimized for Linux. Detected platform: {platform.system()}")
        print()
    
    # PyTorch GPU test
    pytorch_ok = test_pytorch_gpu()
    print()
    
    # Backend info
    check_backend()
    print()
    
    # Summary
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    
    if pytorch_ok:
        print("✅ GPU is properly detected and functional!")
        print("   You can proceed with the Ubuntu/k3d implementation.")
    else:
        print("❌ GPU is not detected or not functional.")
        print("\nNext steps:")
        if is_linux:
            print("1. Install ROCm drivers:")
            print("   https://rocm.docs.amd.com/projects/install-on-linux/en/latest/")
            print("2. Verify PyTorch ROCm installation:")
            print("   uv sync  # or: pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm7.1")
        else:
            print("1. This script is designed for Linux platforms")
            print("2. Please use Linux for full ROCm support")
        print("3. Re-run this script to verify")
    
    print("=" * 60)
    print()

if __name__ == "__main__":
    main()
