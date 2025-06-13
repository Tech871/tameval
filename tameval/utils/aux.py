import os
import sys
import shutil
import pandas as pd
import logging

import subprocess


def setup_logger(
    logger_name: str = "Logger",
    log_file_path: str = "log.txt",
    log_to_terminal: bool = False,
):
    # Create a logger
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    # Create a file handler to write logs to 'log.txt'
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(logging.DEBUG)  # Set the level for the file handler

    # Create a formatter and attach it to the file handler
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    if log_to_terminal:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)  # Set the level for the stream handler
        stream_handler.setFormatter(formatter)  # Use the same formatter
        logger.addHandler(stream_handler)

    return logger


def copy_folder(proj_path: str, new_path: str, clean_folder: bool = False):
    # Check if the source directory exists
    if not os.path.exists(proj_path):
        print(f"Source path '{proj_path}' does not exist.")
        return

    # If the destination directory already exists, clear it first
    if os.path.exists(new_path):
        print(f"Destination path '{new_path}' already exists!")
        if not clean_folder:
            return
        shutil.rmtree(new_path)

    # Copy the entire folder from proj_path to new_path
    try:
        shutil.copytree(proj_path, new_path)
        print(f"Folder copied from '{proj_path}' to '{new_path}' successfully.")
    except Exception as e:
        print(f"An error occurred while copying the folder: {e}")


def copy_file(src: str, dst: str) -> bool:
    """
    Копирует файл из src в dst, проверяя его существование и создавая нужные папки.
    """
    if not os.path.isfile(src):
        raise ValueError(f"Ошибка: Файл '{src}' не найден.")

    dst_folder = os.path.dirname(dst)
    if dst_folder and not os.path.exists(dst_folder):
        os.makedirs(dst_folder)

    shutil.copy(src, dst)
    return True


def write_to_file(path: str, content: str) -> bool:
    print(f"Writing to {path} ...")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True
