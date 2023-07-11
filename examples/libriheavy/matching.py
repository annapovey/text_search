#!/usr/bin/env python3
# Copyright    2023  Xiaomi Corp.        (authors: Wei Kang)
#
# See ../../../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import numpy as np
import os
from datetime import datetime
from multiprocessing.pool import Pool
from multiprocessing.pool import ThreadPool
from pathlib import Path
from typing import Any, Dict, List, Set, Optional, Tuple, Union

from lhotse import CutSet, MonoCut, SupervisionSegment, load_manifest_lazy
from lhotse.serialization import SequentialJsonlWriter
from textsearch import (
    AttributeDict,
    TextSource,
    Transcript,
    SourcedText,
    align_queries,
    append_texts,
    filter_texts,
    is_punctuation,
    split_aligned_queries,
    texts_to_sourced_texts,
)


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest-in",
        type=str,
        help="""The manifest generated by transcript stage containing book path,
        recordings path and recognition results.
        """,
    )
    parser.add_argument(
        "--manifest-out",
        type=str,
        help="""The file name of the new manifests to write to. 
        """,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="""The number of cuts in a batch.
        """,
    )
    return parser.parse_args()


def get_params() -> AttributeDict:
    """Return a dict containing matching parameters.

    All related parameters that are not passed from the commandline
    are saved in the variable `params`.

    Commandline options are merged into `params` after they are parsed, so
    you can also access them via `params`.

    """
    params = AttributeDict(
        {
            # parameters for loading source texts
            # you can find the docs in textsearch/datatypes.py
            "use_utf8": False,
            "is_bpe": True,
            "use_uppercase": True,
            "has_punctuation": True,
            # parameters for aligning queries
            # you can find the docs in textsearch/match.py#align_queries
            "num_close_matches": 2,
            "segment_length": 5000,
            "reference_length_difference": 0.1,
            "min_matched_query_ratio": 0.33,
            # parameters for splitting aligned queries
            # you can find the docs in textsearch/match.py#split_aligned_queries
            "preceding_context_length": 1000,
            "timestamp_position": "current",
            "silence_length_to_break": 0.45,
            "min_duration": 2,
            "max_duration": 30,
            "expected_duration": (5, 20),
            "max_error_rate": 0.20,
        }
    )

    return params


def load_data(
    params: AttributeDict,
    batch_cuts: List[MonoCut],
    worker_index: int = 0,
) -> Dict[str, Any]:
    """
    Load texts (the references) from disk, and construct the sourced_text object.

    Args:
      params:
        The parameters for matching.
      batch_cuts:
        A list of MonoCut red from manifests.
      worker_index:
        A identification of workers, just for logging.

    Return
      Return a dict containing:
        {
            "num_query_tokens": int,        # the total number of tokens of queries
            "cut_indexes": Tuple[int, int], # A tuple of (cut index, supervision index)
            "sourced_text": SourcedText,    # The sourced_text constructed from batch_cuts.
        }
    """
    # List of transcripts (total number of valid supervisions in the cuts).
    transcripts: List[Transcript] = []

    # Contains cut index and local supervision index
    transcripts_cut_index: List[Tuple[int, int]] = []

    # Constructed from the valid books in the cuts
    books: List[TextSource] = []

    # unique books exist in this batch of cuts
    book_paths: Set[str] = set()
    num_query_tokens = 0

    logging.debug(f"Worker[{worker_index}] loading cuts and books")
    # Construct transcripts
    for i, cut in enumerate(batch_cuts):
        # No text book available, skip this cut.
        if not os.path.isfile(cut.text_path):
            logging.warning(
                f"Worker[{worker_index}] Skipping {cut.id} due to missing "
                f"of reference book"
            )
            continue
        for j, sup in enumerate(cut.supervisions):
            # Transcript requires the input to be the dict like this.
            text_list = []
            begin_times_list = []
            for ali in sup.alignment["symbol"]:
                text_list.append(ali.symbol)
                begin_times_list.append(ali.start)
            aligns = {"text": text_list, "begin_times": begin_times_list}
            # alignments in a supervision might be empty
            if aligns["text"]:
                transcript = Transcript.from_dict(
                    name=sup.id,
                    d=aligns,
                    use_utf8=params.use_utf8,
                    is_bpe=params.is_bpe,
                )
                num_query_tokens += transcript.binary_text.size
                transcripts.append(transcript)
                transcripts_cut_index.append((i, j))
        book_paths.add(cut.text_path)

    # Construct references (the books)
    for i, book_path in enumerate(book_paths):
        with open(book_path, "r") as f:
            book_text = f.read()
            book = TextSource.from_str(
                name=book_path,
                s=book_text,
                use_utf8=params.use_utf8,
                has_punctuation=params.has_punctuation,
            )
            books.append(book)

    if not transcripts:
        return {}

    logging.debug(f"Worker[{worker_index}] loading cuts and books done.")

    sourced_transcript_lists = texts_to_sourced_texts(
        transcripts, uppercase=params.use_uppercase
    )
    sourced_transcripts = append_texts(sourced_transcript_lists)

    sourced_book_list = texts_to_sourced_texts(
        books, uppercase=params.use_uppercase
    )
    sourced_books = append_texts(sourced_book_list)

    def _is_not_punc(c: np.int32) -> bool:
        return not is_punctuation(chr(int(c)))

    # Removing the punctuation
    sourced_books = filter_texts(sourced_books, fn=_is_not_punc)

    sourced_text = append_texts([sourced_transcripts, sourced_books])

    logging.debug(f"Worker[{worker_index}] construct sourced_text done.")

    assert num_query_tokens == sourced_text.doc_splits[len(transcripts)], (
        num_query_tokens,
        sourced_text.doc_splits[len(transcripts)],
    )

    return {
        "num_query_tokens": num_query_tokens,
        "cut_indexes": transcripts_cut_index,
        "sourced_text": sourced_text,
    }


def align(
    params: AttributeDict,
    num_query_tokens: int,
    sourced_text: SourcedText,
    thread_pool: ThreadPool,
    worker_index: int = 0,
):
    """
    Align each query in the sourced_text to its corresponding segment in references

    Args:
      params:
        The matching parameters.
      num_query_tokens:
        The number of total query tokens in current batch.
      sourced_text:
        The SourcedText constructed from current batch.

    Return:
      Return a list of tuple containing
      ((query_start, target_start), [alignment item]).
      The `query_start` and `target_start` are indexes in sourced_text,
      the `alignment item` is a list containing `{"ref": ref, "hyp": hyp,
      "ref_pos": ref_pos, "hyp_pos": hyp_pos, "hyp_time": hyp_time}`, `ref` is
      the token from reference, `hyp` is the token from query, `ref_pos` is
      local index in reference document, `hyp_pos` is local index in query
      document, `hyp_time` is the timestamp of `hyp`.
    """
    logging.debug(f"Worker[{worker_index}] Aligning queries.")
    alignments = align_queries(
        sourced_text,
        num_query_tokens=num_query_tokens,
        num_close_matches=params.num_close_matches,
        segment_length=params.segment_length,
        reference_length_difference=params.reference_length_difference,
        min_matched_query_ratio=params.min_matched_query_ratio,
        thread_pool=thread_pool,
    )
    logging.debug(f"Worker[{worker_index}] Aligning queries done.")

    return alignments


def split(
    params: AttributeDict,
    sourced_text: SourcedText,
    alignments,
    cut_indexes: Tuple[int, int],
    process_pool: Optional[Pool] = None,
    worker_index: int = 0,
):
    """
    Split the query into smaller segments.

    Args:
      params:
        The parameters for matching.
      sourced_text:
        An instance of SourcedText.
      alignments:
        Return from function `align`.
      cut_indexes:
        A list of tuple containing the original cut index and supervision index
        of the query([(cut index, sup index)]), it satisfies
        `len(cut_indexes) == len(alignments)`
      process_pool:
        The process pool to split aligned queries. The algorithms are
        implemented in python, so we use process pool to get rid of the effect
        of GIL.
      worker_index:
        A identification of workers, just for logging.

    Return:
      Returns a list of Dict containing the details of each segment, looks like

         {
             "begin_byte": int,   # begin position in original target source
             "end_byte": int,     # end position in original target source
             "start_time": float, # start timestamp in the audio
             "duration": float,   # duration of this segment
             "hyp": str,          # text from query source
             "ref": str,          # text from target source
             "pre_ref": str,      # preceding text from target source
             "pre_hyp": str,      # preceding text from query source
             "post_ref": str,     # succeeding text from target source
             "post_hyp": str,     # succeeding text from query source
         }
    """
    logging.debug(f"Worker[{worker_index}] Splitting aligned query.")
    segments = split_aligned_queries(
        sourced_text=sourced_text,
        alignments=alignments,
        cut_indexes=cut_indexes,
        process_pool=process_pool,
        preceding_context_length=params.preceding_context_length,
        timestamp_position=params.timestamp_position,
        silence_length_to_break=params.silence_length_to_break,
        min_duration=params.min_duration,
        max_duration=params.max_duration,
        expected_duration=params.expected_duration,
        max_error_rate=params.max_error_rate,
    )
    logging.debug(f"Worker[{worker_index}] Splitting aligned query done.")

    return segments


def write(
    batch_cuts: List[MonoCut],
    results,
    cuts_writer: SequentialJsonlWriter,
):
    """
    Write the segmented results to disk as new manifests.

    Args:
      batch_cuts:
        The original batch cuts.
      results:
        Returned from `split`.
      cuts_writer:
        The writer used to write the new manifests out.
    """
    cut_segment_index: Dict[str, int] = {}
    cut_list = []
    for item in results:
        cut_indexes = item[0]
        segments = item[1]

        current_cut = batch_cuts[cut_indexes[0]]
        if current_cut.id not in cut_segment_index:
            cut_segment_index[current_cut.id] = 0

        for seg in segments:
            id = f"{current_cut.id}_{cut_segment_index[current_cut.id]}"
            cut_segment_index[current_cut.id] += 1
            supervision = SupervisionSegment(
                id=id,
                channel=current_cut.supervisions[cut_indexes[1]].channel,
                language=current_cut.supervisions[cut_indexes[1]].language,
                speaker=current_cut.supervisions[cut_indexes[1]].speaker,
                recording_id=current_cut.recording.id,
                start=0,
                duration=seg["duration"],
                custom={
                    "texts": [seg["ref"], seg["hyp"]],
                    "pre_texts": [seg["pre_ref"], seg["pre_hyp"]],
                    "post_texts": [seg["post_ref"], seg["post_ref"]],
                    "begin_byte": seg["begin_byte"],
                    "end_byte": seg["end_byte"],
                },
            )
            cut = MonoCut(
                id,
                start=seg["start_time"],
                duration=seg["duration"],
                channel=current_cut.channel,
                supervisions=[supervision],
                recording=current_cut.recording,
                custom={"text_path": str(current_cut.text_path)},
            )
            cut_list.append(cut)

    logging.debug(f"Writing results.")
    for i, cut in enumerate(cut_list):
        # Flushing only on last cut to accelerate writing.
        cuts_writer.write(cut, flush=(i == len(cut_list) - 1))
    logging.debug(f"Write results done.")


def process_one_batch(
    params: AttributeDict,
    batch_cuts: List[MonoCut],
    thread_pool: ThreadPool,
    process_pool: Pool,
    cuts_writer: SequentialJsonlWriter,
):
    raw_data = load_data(params, batch_cuts=batch_cuts)
    if len(raw_data) == 0:
        logging.warning("Raw data is empty.")
        return
    aligned_data = align(
        params,
        num_query_tokens=raw_data["num_query_tokens"],
        sourced_text=raw_data["sourced_text"],
        thread_pool=thread_pool,
    )
    if len(aligned_data) == 0:
        logging.warning("Aligned data is empty.")
        return
    splited_data = split(
        params,
        sourced_text=raw_data["sourced_text"],
        alignments=aligned_data,
        cut_indexes=raw_data["cut_indexes"],
        process_pool=process_pool,
    )
    if len(splited_data) == 0:
        logging.warning("Splitted data is empty.")
        return
    write(
        batch_cuts=batch_cuts,
        results=splited_data,
        cuts_writer=cuts_writer,
    )


def main():
    args = get_args()
    params = get_params()
    params.update(vars(args))

    logging.info(f"params : {params}")

    raw_cuts = load_manifest_lazy(params.manifest_in)
    cuts_writer = CutSet.open_writer(params.manifest_out, overwrite=True)

    # thread_pool to run the levenshtein alignment.
    # we use thread_pool here because the levenshtein run on C++ with GIL released.
    thread_pool = ThreadPool()

    # process_pool to split the query into segments.
    # we use process_pool here because the splitting algorithm run on Python
    # (we can not get accelerating with thread_pool because of the GIL)
    process_pool = Pool()

    batch_cuts = []
    logging.info(f"Start processing...")
    for i, cut in enumerate(raw_cuts):
        if len(batch_cuts) < params.batch_size:
            batch_cuts.append(cut)
        else:
            process_one_batch(
                params,
                batch_cuts=batch_cuts,
                thread_pool=thread_pool,
                process_pool=process_pool,
                cuts_writer=cuts_writer,
            )
            batch_cuts = []
            logging.info(f"Number of cuts have been loaded is {i}")
    if len(batch_cuts):
        process_one_batch(
            params,
            batch_cuts=batch_cuts,
            thread_pool=thread_pool,
            process_pool=process_pool,
            cuts_writer=cuts_writer,
        )


if __name__ == "__main__":
    formatter = (
        "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    )
    now = datetime.now()
    data_time = now.strftime("%Y-%m-%d-%H-%M-%S")
    os.makedirs("logs", exist_ok=True)
    log_file_name = f"logs/matching_{data_time}"
    logging.basicConfig(
        level=logging.INFO,
        format=formatter,
        handlers=[logging.FileHandler(log_file_name), logging.StreamHandler()],
    )

    main()
