import sys
import torch
import torch.distributed

# Monkeypatch for ROCm Windows compatibility with peft
if not hasattr(torch.distributed, "tensor"):
    class Dummy:
        pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling,
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from datasets import load_dataset

def train():
    model_id = "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T"
    
    # 1. Load Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.pad_token = tokenizer.eos_token
    
    # 2. Load Model
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        dtype=torch.float16,
        device_map="auto",
    )
    
    # 3. Prepare for LoRA
    config = LoraConfig(
        r=16, # Increased rank
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM"
    )
    
    model = get_peft_model(model, config)
    model.print_trainable_parameters()
    
    # 4. Load Dataset
    dataset = load_dataset("json", data_files="data/train.jsonl", split="train")
    
    def tokenize_function(examples):
        return tokenizer(examples["text"], truncation=True, padding="max_length", max_length=512)
    
    tokenized_dataset = dataset.map(tokenize_function, batched=True, remove_columns=["text"])
    
    # 5. Training Arguments
    training_args = TrainingArguments(
        output_dir="./lora-tiny-llama",
        per_device_train_batch_size=4,
        gradient_accumulation_steps=4,
        learning_rate=3e-4,
        lr_scheduler_type="cosine",
        warmup_steps=50,
        weight_decay=0.01,
        logging_steps=10,
        max_steps=300,
        save_steps=100,
        fp16=True,
        push_to_hub=False,
        report_to="none"
    )
    
    # 6. Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_dataset,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
    )
    
    print("Starting training...")
    trainer.train()
    
    # 7. Save Adapter
    model.save_pretrained("./lora-tiny-llama-adapter")
    print("Training complete. Adapter saved.")
    
    # Cleanup to prevent hangs on ROCm Windows
    del model
    del trainer
    torch.cuda.empty_cache()
    sys.exit(0)

if __name__ == "__main__":
    train()
