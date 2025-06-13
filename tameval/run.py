import argparse


import setup.run as teval_setup
import generate.run as teval_generate
import teval.run as teval_eval


def parse_args():
    parent_parser = argparse.ArgumentParser(add_help=False)
    setup_parser = teval_setup.get_arg_parser_obj(parents=[parent_parser])
    gen_parser = teval_generate.get_arg_parser_obj(parents=[parent_parser])
    eval_parser = teval_eval.get_arg_parser_obj(parents=[parent_parser])
    all_args = {}

    args, _ = setup_parser.parse_known_args()
    all_args.update(vars(args))

    args, _ = gen_parser.parse_known_args()
    all_args.update(vars(args))

    args, _ = eval_parser.parse_known_args()
    all_args.update(vars(args))

    combined_args = argparse.Namespace(**all_args)
    return combined_args


if __name__ == "__main__":
    args = parse_args()

    for attempt in range(1, args.max_attempts_num + 1):
        print(
            f"\n\n\n------ ATTEMPT {attempt} STARTED for {args.model_to_eval} ------\n\n\n"
        )
        args.attempt = attempt

        print(f"\n------ SETUP STARTED ({attempt}) for {args.model_to_eval}------\n")
        # 1. Setup repos if not built already
        # at least refresh code files from dowload repo
        if attempt > 1:
            args.only_refresh_files = 1
        teval_setup.main(args)

        # 2. Generate enhanced test files
        print(
            f"\n------ GENERATION STARTED ({attempt}) for {args.model_to_eval}------\n"
        )
        if args.model_to_eval == "dry-run":
            print("Dry run...")
            teval_generate.dry_run(args)
        elif args.batch_generation:
            print("Batch generation mode activated!")
            teval_generate.main_batch(args)
        else:
            teval_generate.main(args)

        # # 3. Evaluate enhanced test files
        print(f"\n------ EVAL STARTED ({attempt}) for {args.model_to_eval}------\n")
        teval_eval.main(args)
