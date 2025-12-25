import os
from datasets import load_dataset

def prepare_data():
    print("Loading dataset...")
    # Using a small subset of dolly-15k for demonstration
    dataset = load_dataset("databricks/databricks-dolly-15k", split="train[:1000]")
    
    def format_instruction(sample):
        return {
            "text": f"### Instruction:\n{sample['instruction']}\n\n### Input:\n{sample['context']}\n\n### Response:\n{sample['response']}"
        }

    print("Formatting dataset...")
    dataset = dataset.map(format_instruction, remove_columns=dataset.column_names)
    
    # Add specific LoRA (Low-Rank Adaptation) samples
    custom_samples = [
        {
            "text": "### Instruction:\nExplain what LoRA is in one sentence.\n\n### Input:\n\n### Response:\nLoRA (Low-Rank Adaptation) is a fine-tuning technique that reduces the number of trainable parameters by injecting low-rank matrices into the model layers."
        },
        {
            "text": "### Instruction:\nWhat are the benefits of LoRA?\n\n### Input:\n\n### Response:\nLoRA reduces VRAM usage, speeds up training, and allows for efficient switching between different fine-tuned adapters."
        },
        {
            "text": "### Instruction:\nHow does LoRA work?\n\n### Response:\nLoRA works by freezing the pre-trained model weights and adding trainable low-rank decomposition matrices to each layer of the Transformer architecture."
        }
    ]
    
    import json
    # Save to local jsonl
    output_path = "data/sft_train.jsonl"
    os.makedirs("data", exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        # Write Dolly samples
        for sample in dataset:
            f.write(json.dumps(sample) + "\n")
        # Write custom samples multiple times to increase weight
        for _ in range(20): # Repeat 20 times to ensure the model sees it
            for sample in custom_samples:
                f.write(json.dumps(sample) + "\n")
                
    print(f"Dataset saved to {output_path} (included custom LoRA samples)")

if __name__ == "__main__":
    prepare_data()
