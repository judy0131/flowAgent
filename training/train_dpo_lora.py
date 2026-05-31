import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Train LoRA policy with DPO on plan preference pairs.")
    parser.add_argument("--model-name", default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("--pairs-path", type=Path, default=Path("data/preferences/dpo_pairs.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/dpo_lora"))
    parser.add_argument("--beta", type=float, default=0.1)
    parser.add_argument("--lr", type=float, default=5e-6)
    parser.add_argument("--epochs", type=int, default=1)
    args = parser.parse_args()

    try:
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except Exception as e:
        raise RuntimeError(
            "Missing training dependencies. Install: transformers datasets trl peft accelerate bitsandbytes"
        ) from e

    if not args.pairs_path.exists():
        raise FileNotFoundError(f"Pairs file not found: {args.pairs_path}")

    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=False)
    model = AutoModelForCausalLM.from_pretrained(args.model_name)
    ref_model = AutoModelForCausalLM.from_pretrained(args.model_name)

    lora_cfg = LoraConfig(
        r=8,
        lora_alpha=16,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        task_type="CAUSAL_LM",
    )

    dataset = load_dataset("json", data_files=str(args.pairs_path), split="train")

    train_cfg = DPOConfig(
        output_dir=str(args.output_dir),
        beta=args.beta,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        logging_steps=10,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        save_strategy="epoch",
        fp16=False,
        bf16=False,
    )

    trainer = DPOTrainer(
        model=model,
        ref_model=ref_model,
        args=train_cfg,
        train_dataset=dataset,
        tokenizer=tokenizer,
        peft_config=lora_cfg,
    )
    trainer.train()
    trainer.save_model(str(args.output_dir))
    print(f"DPO LoRA adapter saved to: {args.output_dir}")


if __name__ == "__main__":
    main()

