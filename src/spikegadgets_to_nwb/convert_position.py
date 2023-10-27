import logging
import os
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pandas as pd
from pynwb import NWBFile, TimeSeries
from pynwb.behavior import BehavioralEvents, Position
from pynwb.image import ImageSeries
from scipy.ndimage import label
from scipy.stats import linregress

NANOSECONDS_PER_SECOND = 1e9


def parse_dtype(fieldstr: str) -> np.dtype:
    """
    Parses the last fields parameter (<time uint32><...>) as a single string.
    Assumes it is formatted as <name number * type> or <name type>. Returns a numpy dtype object.

    Parameters
    ----------
    fieldstr : str
        The string to parse.

    Returns
    -------
    np.dtype
        The numpy dtype object.

    Raises
    ------
    AttributeError
        If the field type is not valid.

    Examples
    --------
    >>> fieldstr = '<time uint32><x float32><y float32><z float32>'
    >>> parse_dtype(fieldstr)
    dtype([('time', '<u4'), ('x', '<f4'), ('y', '<f4'), ('z', '<f4')])

    """
    # Returns np.dtype from field string
    sep = " ".join(
        fieldstr.replace("><", " ").replace(">", " ").replace("<", " ").split()
    ).split()

    typearr = []

    # Every two elemets is fieldname followed by datatype
    for i in range(0, len(sep), 2):
        fieldname = sep[i]
        repeats = 1
        ftype = "uint32"
        # Finds if a <num>* is included in datatype
        if "*" in sep[i + 1]:
            temptypes = re.split("\*", sep[i + 1])
            # Results in the correct assignment, whether str is num*dtype or dtype*num
            ftype = temptypes[temptypes[0].isdigit()]
            repeats = int(temptypes[temptypes[1].isdigit()])
        else:
            ftype = sep[i + 1]
        try:
            fieldtype = getattr(np, ftype)
        except AttributeError:
            raise AttributeError(ftype + " is not a valid field type.\n")
        else:
            typearr.append((str(fieldname), fieldtype, repeats))

    return np.dtype(typearr)


def read_trodes_datafile(filename: Path) -> dict:
    """
    Read trodes binary.

    Parameters
    ----------
    filename : Path
        Path to the trodes binary file.

    Returns
    -------
    dict
        A dictionary containing the settings and data from the trodes binary file.

    Raises
    ------
    Exception
        If the settings format is not supported.

    """
    with open(filename, "rb") as file:
        # Check if first line is start of settings block
        if file.readline().decode().strip() != "<Start settings>":
            raise Exception("Settings format not supported")
        fields_text = dict()
        for line in file:
            # Read through block of settings
            line = line.decode().strip()
            # filling in fields dict
            if line != "<End settings>":
                settings_name, setting = line.split(": ")
                fields_text[settings_name.lower()] = setting
            # End of settings block, signal end of fields
            else:
                break
        # Reads rest of file at once, using dtype format generated by parse_dtype()
        try:
            fields_text["data"] = np.fromfile(
                file, dtype=parse_dtype(fields_text["fields"])
            )
        except KeyError:
            fields_text["data"] = np.fromfile(file)
        return fields_text


def get_framerate(timestamps: np.ndarray) -> float:
    """
    Calculates the framerate of a video based on the timestamps of each frame.

    Parameters
    ----------
    timestamps : np.ndarray
        An array of timestamps for each frame in the video, units = nanoseconds.

    Returns
    -------
    frame_rate: float
        The framerate of the video in frames per second.
    """
    timestamps = np.asarray(timestamps)
    return NANOSECONDS_PER_SECOND / np.median(np.diff(timestamps))


def find_acquisition_timing_pause(
    timestamps: np.ndarray,
    min_duration: float = 0.4,
    max_duration: float = 1.0,
    n_search: int = 100,
) -> float:
    """
    Find the midpoint time of a timing pause in the video stream.

    Parameters
    ----------
    timestamps : np.ndarray
        An array of timestamps for each frame in the video. Expects units=nanoseconds.
    min_duration : float, optional
        The minimum duration of the pause in seconds, by default 0.4.
    max_duration : float, optional
        The maximum duration of the pause in seconds, by default 1.0.
    n_search : int, optional
        The number of frames to search for the pause, by default 100.

    Returns
    -------
    pause_mid_time : float
        The midpoint time of the timing pause in nanoseconds.

    """
    timestamps = np.asarray(timestamps)
    timestamp_difference = np.diff(timestamps[:n_search] / NANOSECONDS_PER_SECOND)
    is_valid_gap = (timestamp_difference > min_duration) & (
        timestamp_difference < max_duration
    )
    pause_start_ind = np.nonzero(is_valid_gap)[0][0]
    pause_end_ind = pause_start_ind + 1
    pause_mid_time = (
        timestamps[pause_start_ind]
        + (timestamps[pause_end_ind] - timestamps[pause_start_ind]) // 2
    )

    return pause_mid_time


def find_large_frame_jumps(
    frame_count: np.ndarray, min_frame_jump: int = 15
) -> np.ndarray:
    """
    Find large frame jumps in the video.

    Parameters
    ----------
    frame_count : np.ndarray
        An array of frame counts for each frame in the video.
    min_frame_jump : int, optional
        The minimum number of frames to consider a jump as large, by default 15.

    Returns
    -------
    np.ndarray
        A boolean array indicating whether each frame has a large jump.

    """
    logger = logging.getLogger("convert")
    frame_count = np.asarray(frame_count)

    is_large_frame_jump = np.insert(np.diff(frame_count) > min_frame_jump, 0, False)

    logger.info(f"big frame jumps: {np.nonzero(is_large_frame_jump)[0]}")

    return is_large_frame_jump


def detect_repeat_timestamps(timestamps: np.ndarray) -> np.ndarray:
    """
    Detects repeated timestamps in an array of timestamps.

    Parameters
    ----------
    timestamps : np.ndarray
        Array of timestamps.

    Returns
    -------
    np.ndarray
        Boolean array indicating whether each timestamp is repeated.
    """
    return np.insert(timestamps[:-1] >= timestamps[1:], 0, False)


import numpy as np


def detect_trodes_time_repeats_or_frame_jumps(
    trodes_time: np.ndarray, frame_count: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """
    Detects if a Trodes time index repeats, indicating that the Trodes clock has frozen
    due to headstage disconnects. Also detects large frame jumps.

    Parameters
    ----------
    trodes_time : np.ndarray
        Array of Trodes time indices.
    frame_count : np.ndarray
        Array of frame counts.

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        A tuple containing two arrays:
        - non_repeat_timestamp_labels : np.ndarray
            Array of labels for non-repeating timestamps.
        - non_repeat_timestamp_labels_id : np.ndarray
            Array of unique IDs for non-repeating timestamps.
    """
    logger = logging.getLogger("convert")

    trodes_time = np.asarray(trodes_time)
    is_repeat_timestamp = detect_repeat_timestamps(trodes_time)
    logger.info(f"repeat timestamps ind: {np.nonzero(is_repeat_timestamp)[0]}")

    is_large_frame_jump = find_large_frame_jumps(frame_count)
    is_repeat_timestamp = np.logical_or(is_repeat_timestamp, is_large_frame_jump)

    repeat_timestamp_labels = label(is_repeat_timestamp)[0]
    repeat_timestamp_labels_id, repeat_timestamp_label_counts = np.unique(
        repeat_timestamp_labels, return_counts=True
    )
    is_repeat = np.logical_and(
        repeat_timestamp_labels_id != 0, repeat_timestamp_label_counts > 2
    )
    repeat_timestamp_labels_id = repeat_timestamp_labels_id[is_repeat]
    repeat_timestamp_label_counts = repeat_timestamp_label_counts[is_repeat]
    is_repeat_timestamp[
        ~np.isin(repeat_timestamp_labels, repeat_timestamp_labels_id)
    ] = False

    non_repeat_timestamp_labels = label(~is_repeat_timestamp)[0]
    non_repeat_timestamp_labels_id = np.unique(non_repeat_timestamp_labels)
    non_repeat_timestamp_labels_id = non_repeat_timestamp_labels_id[
        non_repeat_timestamp_labels_id != 0
    ]

    return (non_repeat_timestamp_labels, non_repeat_timestamp_labels_id)


def estimate_camera_time_from_mcu_time(
    position_timestamps: np.ndarray, mcu_timestamps: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """

    Parameters
    ----------
    position_timestamps : pd.DataFrame
    mcu_timestamps : pd.DataFrame

    Returns
    -------
    camera_systime : np.ndarray, shape (n_frames_within_neural_time,)
    is_valid_camera_time : np.ndarray, shape (n_frames,)

    """
    is_valid_camera_time = np.isin(position_timestamps.index, mcu_timestamps.index)
    camera_systime = np.asarray(
        mcu_timestamps.loc[position_timestamps.index[is_valid_camera_time]]
    )

    return camera_systime, is_valid_camera_time


def estimate_camera_to_mcu_lag(
    camera_systime: np.ndarray, dio_systime: np.ndarray, n_breaks: int = 0
) -> float:
    logger = logging.getLogger("convert")
    if n_breaks == 0:
        dio_systime = dio_systime[: len(camera_systime)]
        camera_to_mcu_lag = np.median(camera_systime - dio_systime)
    else:
        camera_to_mcu_lag = camera_systime[0] - dio_systime[0]

    logger.info(
        "estimated trodes to camera lag: "
        f"{camera_to_mcu_lag / NANOSECONDS_PER_SECOND:0.3f} s"
    )
    return camera_to_mcu_lag


def remove_acquisition_timing_pause_non_ptp(
    dio_systime: np.ndarray,
    frame_count: np.ndarray,
    camera_systime: np.ndarray,
    is_valid_camera_time: np.ndarray,
    pause_mid_time: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Remove acquisition timing pause non-PTP.

    Parameters
    ----------
    dio_systime : np.ndarray
        Digital I/O system time.
    frame_count : np.ndarray
        Frame count.
    camera_systime : np.ndarray
        Camera system time.
    is_valid_camera_time : np.ndarray
        Boolean array indicating whether the camera time is valid.
    pause_mid_time : float
        Midpoint time of the pause.

    Returns
    -------
    tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
        A tuple containing the following arrays:
        - dio_systime : np.ndarray
            Digital I/O system time after removing the pause.
        - frame_count : np.ndarray
            Frame count after removing the pause.
        - is_valid_camera_time : np.ndarray
            Boolean array indicating whether the camera time is valid after removing the pause.
        - camera_systime : np.ndarray
            Camera system time after removing the pause.
    """
    dio_systime = dio_systime[dio_systime > pause_mid_time]
    frame_count = frame_count[is_valid_camera_time][camera_systime > pause_mid_time]
    is_valid_camera_time[is_valid_camera_time] = camera_systime > pause_mid_time
    camera_systime = camera_systime[camera_systime > pause_mid_time]

    return dio_systime, frame_count, is_valid_camera_time, camera_systime


def correct_timestamps_for_camera_to_mcu_lag(
    frame_count: np.ndarray, camera_systime: np.ndarray, camera_to_mcu_lag: np.ndarray
) -> np.ndarray:
    regression_result = linregress(frame_count, camera_systime - camera_to_mcu_lag)
    corrected_camera_systime = (
        regression_result.intercept + frame_count * regression_result.slope
    )
    # corrected_camera_systime /= NANOSECONDS_PER_SECOND

    return corrected_camera_systime


def find_camera_dio_channel(nwb_file):
    dio_camera_name = [
        key
        for key in nwb_file.processing["behavior"]
        .data_interfaces["behavioral_events"]
        .time_series
        if "camera ticks" in key
    ]
    if len(dio_camera_name) > 1:
        raise ValueError(
            "Multiple camera dio channels found by name. Not implemented for multiple cameras without PTP yet."
        )

    if len(dio_camera_name) == 0:
        raise ValueError(
            "No camera dio channel found by name. Check metadata YAML. Name must contain 'camera ticks'"
        )

    return (
        nwb_file.processing["behavior"]
        .data_interfaces["behavioral_events"]
        .time_series[dio_camera_name[0]]
        .timestamps
    )


def get_video_timestamps(video_timestamps_filepath: Path) -> np.ndarray:
    """
    Get video timestamps.

    Parameters
    ----------
    video_timestamps_filepath : Path
        Path to the video timestamps file.

    Returns
    -------
    np.ndarray
        An array of video timestamps.
    """
    # Get video timestamps
    video_timestamps = (
        pd.DataFrame(read_trodes_datafile(video_timestamps_filepath)["data"])
        .set_index("PosTimestamp")
        .rename(columns={"frameCount": "HWframeCount"})
    )
    return (
        np.asarray(video_timestamps.HWTimestamp, dtype=np.float64)
        / NANOSECONDS_PER_SECOND
    )


def get_position_timestamps(
    position_timestamps_filepath: Path,
    position_tracking_filepath=None | Path,
    rec_dci_timestamps=None | np.ndarray,
    dio_camera_timestamps=None | np.ndarray,
    sample_count=None | np.ndarray,
    ptp_enabled: bool = True,
):
    logger = logging.getLogger("convert")

    # Get video timestamps
    video_timestamps = (
        pd.DataFrame(read_trodes_datafile(position_timestamps_filepath)["data"])
        .set_index("PosTimestamp")
        .rename(columns={"frameCount": "HWframeCount"})
    )

    # On AVT cameras, HWFrame counts wraps to 0 above this value.
    video_timestamps["HWframeCount"] = np.unwrap(
        video_timestamps["HWframeCount"].astype(np.int32),
        period=np.iinfo(np.uint16).max,
    )
    # Keep track of video frames
    video_timestamps["video_frame_ind"] = np.arange(len(video_timestamps))

    # Disconnects manifest as repeats in the trodes time index
    (
        non_repeat_timestamp_labels,
        non_repeat_timestamp_labels_id,
    ) = detect_trodes_time_repeats_or_frame_jumps(
        video_timestamps.index, video_timestamps.HWframeCount
    )
    logger.info(f"\tnon_repeat_timestamp_labels = {non_repeat_timestamp_labels_id}")
    video_timestamps["non_repeat_timestamp_labels"] = non_repeat_timestamp_labels
    video_timestamps = video_timestamps.loc[
        video_timestamps.non_repeat_timestamp_labels > 0
    ]

    # Get position tracking information
    try:
        position_tracking = pd.DataFrame(
            read_trodes_datafile(position_tracking_filepath)["data"]
        ).set_index("time")
        is_repeat_timestamp = detect_repeat_timestamps(position_tracking.index)
        position_tracking = position_tracking.iloc[~is_repeat_timestamp]

        # Match the camera frames to the position tracking
        # Number of video frames can be different from online tracking because
        # online tracking can be started or stopped before video is stopped.
        # Additionally, for offline tracking, frames can be skipped if the
        # frame is labeled as bad.
        video_timestamps = pd.merge(
            video_timestamps,
            position_tracking,
            right_index=True,
            left_index=True,
            how="left",
        )
    except (FileNotFoundError, TypeError):
        pass

    if ptp_enabled:
        ptp_systime = np.asarray(video_timestamps.HWTimestamp)
        # Convert from integer nanoseconds to float seconds
        ptp_timestamps = pd.Index(ptp_systime / NANOSECONDS_PER_SECOND, name="time")
        video_timestamps = video_timestamps.drop(
            columns=["HWframeCount", "HWTimestamp"]
        ).set_index(ptp_timestamps)

        # Ignore positions before the timing pause.
        pause_mid_ind = (
            np.nonzero(
                np.logical_and(
                    np.diff(video_timestamps.index[:100]) > 0.4,
                    np.diff(video_timestamps.index[:100]) < 2.0,
                )
            )[0][0]
            + 1
        )
        video_timestamps = video_timestamps.iloc[pause_mid_ind:]
        logger.info(
            "Camera frame rate estimated from MCU timestamps:"
            f" {1 / np.median(np.diff(video_timestamps.index)):0.1f} frames/s"
        )
        return video_timestamps
    else:
        try:
            pause_mid_time = (
                find_acquisition_timing_pause(
                    dio_camera_timestamps * NANOSECONDS_PER_SECOND
                )
                / NANOSECONDS_PER_SECOND
            )
            frame_rate_from_dio = get_framerate(
                dio_camera_timestamps[dio_camera_timestamps > pause_mid_time]
            )
            logger.info(
                "Camera frame rate estimated from DIO camera ticks:"
                f" {frame_rate_from_dio:0.1f} frames/s"
            )
        except IndexError:
            pause_mid_time = -1

        frame_count = np.asarray(video_timestamps.HWframeCount)

        is_valid_camera_time = np.isin(video_timestamps.index, sample_count)

        camera_systime = rec_dci_timestamps[
            np.digitize(video_timestamps.index[is_valid_camera_time], sample_count)
        ]
        (
            dio_camera_timestamps,
            frame_count,
            is_valid_camera_time,
            camera_systime,
        ) = remove_acquisition_timing_pause_non_ptp(
            dio_camera_timestamps,
            frame_count,
            camera_systime,
            is_valid_camera_time,
            pause_mid_time,
        )
        video_timestamps = video_timestamps.iloc[is_valid_camera_time]
        frame_rate_from_camera_systime = get_framerate(camera_systime)
        logger.info(
            "Camera frame rate estimated from camera sys time:"
            f" {frame_rate_from_camera_systime:0.1f} frames/s"
        )
        camera_to_mcu_lag = estimate_camera_to_mcu_lag(
            camera_systime, dio_camera_timestamps, len(non_repeat_timestamp_labels_id)
        )
        corrected_camera_systime = []
        for id in non_repeat_timestamp_labels_id:
            is_chunk = video_timestamps.non_repeat_timestamp_labels == id
            corrected_camera_systime.append(
                correct_timestamps_for_camera_to_mcu_lag(
                    frame_count[is_chunk],
                    camera_systime[is_chunk],
                    camera_to_mcu_lag,
                )
            )
        corrected_camera_systime = np.concatenate(corrected_camera_systime)

        video_timestamps = video_timestamps.set_index(
            pd.Index(corrected_camera_systime, name="time")
        )
        return video_timestamps.groupby(
            video_timestamps.index
        ).first()  # TODO: Figure out why duplicate timesteps make it to this point and why this line is necessary


def find_camera_dio_channel_per_epoch(
    nwb_file: NWBFile, epoch_start: float, epoch_end: float
):
    """Find the camera dio channel for a given epoch.
    Searches through dio channels with "camera ticks" in the name.
    Selects first one with at least 100 ticks in the epoch.

    Parameters
    ----------
    nwb_file : NWBFile
        The NWBFile to find the dio channel in.
    epoch_start : float
        timestamp of the start of the epoch
    epoch_end : float
        timestamp of the end of the epoch

    Returns
    -------
    dio_camera_timestamps : np.ndarray
        The dio timestamps for the camera restricted to the epoch of interest

    Raises
    ------
    ValueError
        Error if dio's  are not added to the nwbfile
    ValueError
        Error if no camera dio channel is found
    """
    dio_camera_list = [
        key
        for key in nwb_file.processing["behavior"]["behavioral_events"].time_series
        if "camera ticks" in key
    ]
    if not dio_camera_list:
        raise ValueError(
            "No camera dio channel found by name. Check metadata YAML. Name must contain 'camera ticks'"
        )
    for camera in dio_camera_list:
        dio_camera_timestamps = (
            nwb_file.processing["behavior"]["behavioral_events"]
            .time_series[camera]
            .timestamps
        )
        epoch_ind = np.logical_and(
            dio_camera_timestamps >= epoch_start, dio_camera_timestamps <= epoch_end
        )
        if np.sum(epoch_ind) > 100:
            return dio_camera_timestamps[epoch_ind]
    raise ValueError("No camera dio has sufficient ticks for this epoch")


def add_position(
    nwb_file: NWBFile,
    metadata: dict,
    session_df: pd.DataFrame,
    ptp_enabled: bool = True,
    rec_dci_timestamps: np.ndarray | None = None,
    sample_count: np.ndarray | None = None,
):
    """
    Add position data to an NWBFile.

    Parameters
    ----------
    nwb_file : NWBFile
        The NWBFile to add the position data to.
    metadata : dict
        Metadata about the experiment.
    session_df : pd.DataFrame
        A DataFrame containing information about the session.
    ptp_enabled : bool, optional
        Whether PTP was enabled, by default True.
    rec_dci_timestamps : np.ndarray, optional
        The recording timestamps, by default None. Only used if ptp not enabled.
    sample_count : np.ndarray, optional
        The trodes sample count, by default None. Only used if ptp not enabled.
    """
    logger = logging.getLogger("convert")

    LED_POS_NAMES = [
        [
            "xloc",
            "yloc",
        ],  # led 0
        [
            "xloc2",
            "yloc2",
        ],
    ]  # led 1

    camera_id_to_meters_per_pixel = {
        camera["id"]: camera["meters_per_pixel"] for camera in metadata["cameras"]
    }

    df = []
    for task in metadata["tasks"]:
        df.append(
            pd.DataFrame(
                [(task["camera_id"], epoch) for epoch in task["task_epochs"]],
                columns=["camera_id", "epoch"],
            )
        )

    epoch_to_camera_ids = pd.concat(df).set_index("epoch").sort_index()

    position = Position(name="position")

    # Make a processing module for behavior and add to the nwbfile
    if not "behavior" in nwb_file.processing:
        nwb_file.create_processing_module(
            name="behavior", description="Contains all behavior-related data"
        )
    # get epoch data to seperate dio timestamps into epochs
    if (not ptp_enabled) and (not len(nwb_file.epochs)):
        raise ValueError(
            "add_epochs() must be run before add_position() for non-ptp data"
        )
    if not ptp_enabled:
        epoch_df = nwb_file.epochs.to_dataframe()

    for epoch in session_df.epoch.unique():
        try:
            position_tracking_filepath = session_df.loc[
                np.logical_and(
                    session_df.epoch == epoch,
                    session_df.file_extension == ".videoPositionTracking",
                )
            ].full_path.to_list()[0]
            # find the matching hw timestamps filepath
            video_index = position_tracking_filepath.split(".")[-2]
            video_hw_df = session_df.loc[
                np.logical_and(
                    session_df.epoch == epoch,
                    session_df.file_extension == ".cameraHWSync",
                )
            ]
            position_timestamps_filepath = video_hw_df[
                [
                    full_path.split(".")[-3] == video_index
                    for full_path in video_hw_df.full_path
                ]
            ].full_path.to_list()[0]

        except IndexError:
            position_tracking_filepath = None

        logger.info(epoch)
        logger.info(f"\tposition_timestamps_filepath: {position_timestamps_filepath}")
        logger.info(f"\tposition_tracking_filepath: {position_tracking_filepath}")

        # restrict dio camera timestamps to the current epoch
        if not ptp_enabled:
            epoch_start = epoch_df[epoch_df.index == epoch - 1]["start_time"].iloc[0]
            epoch_end = epoch_df[epoch_df.index == epoch - 1]["stop_time"].iloc[0]
            dio_camera_timestamps_epoch = find_camera_dio_channel_per_epoch(
                nwb_file=nwb_file, epoch_start=epoch_start, epoch_end=epoch_end
            )
        else:
            dio_camera_timestamps_epoch = None

        position_df = get_position_timestamps(
            position_timestamps_filepath,
            position_tracking_filepath,
            ptp_enabled=ptp_enabled,
            rec_dci_timestamps=rec_dci_timestamps,
            dio_camera_timestamps=dio_camera_timestamps_epoch,
            sample_count=sample_count,
        )

        # TODO: Doesn't handle multiple cameras currently
        camera_id = epoch_to_camera_ids.loc[epoch].camera_id[0]
        meters_per_pixel = camera_id_to_meters_per_pixel[camera_id]

        if position_tracking_filepath is not None:
            for led_number, valid_keys in enumerate(LED_POS_NAMES):
                key_set = [
                    key for key in position_df.columns.tolist() if key in valid_keys
                ]
                if len(key_set) > 0:
                    position.create_spatial_series(
                        name=f"led_{led_number}_series_{epoch}",
                        description=", ".join(["xloc", "yloc"]),
                        data=np.asarray(position_df[key_set]),
                        conversion=meters_per_pixel,
                        reference_frame="Upper left corner of video frame",
                        timestamps=np.asarray(position_df.index),
                    )
        else:
            position.create_spatial_series(
                name=f"series_{epoch}",
                description=", ".join(["xloc", "yloc"]),
                data=np.asarray([]),
                conversion=meters_per_pixel,
                reference_frame="Upper left corner of video frame",
                timestamps=np.asarray(position_df.index),
            )

        # add the video frame index as a new processing module
        if "position_frame_index" not in nwb_file.processing:
            nwb_file.create_processing_module(
                name="position_frame_index",
                description="stores video frame index for each position timestep",
            )
        # add timeseries for each frame index set (once per series because led's share timestamps)
        nwb_file.processing["position_frame_index"].add(
            TimeSeries(
                name=f"series_{epoch}",
                data=np.asarray(position_df["video_frame_ind"]),
                unit="N/A",
                timestamps=np.asarray(position_df.index),
            )
        )
        # add the video non-repeat timestamp labels as a new processing module
        if "non_repeat_timestamp_labels" not in nwb_file.processing:
            nwb_file.create_processing_module(
                name="non_repeat_timestamp_labels",
                description="stores non_repeat_labels for each position timestep",
            )
        # add timeseries for each non-repeat timestamp labels set (once per series because led's share timestamps)
        nwb_file.processing["non_repeat_timestamp_labels"].add(
            TimeSeries(
                name=f"series_{epoch}",
                data=np.asarray(position_df["non_repeat_timestamp_labels"]),
                unit="N/A",
                timestamps=np.asarray(position_df.index),
            )
        )

    nwb_file.processing["behavior"].add(position)


def convert_h264_to_mp4(file: str, video_directory: str) -> str:
    """
    Converts h264 file to mp4 file using ffmpeg.

    Parameters
    ----------
    file : str
        The path to the input h264 file.
    video_directory : str
        Where to save the output mp4 file.

    Returns
    -------
    str
        The path to the output mp4 file.

    Raises
    ------
    subprocess.CalledProcessError
        If the ffmpeg command fails.

    """
    new_file_name = file.replace(".h264", ".mp4")
    new_file_name = video_directory + new_file_name.split("/")[-1]
    logger = logging.getLogger("convert")
    if os.path.exists(new_file_name):
        return new_file_name
    try:
        # Construct the ffmpeg command
        subprocess.run(f"ffmpeg -i {file} {new_file_name}", shell=True)
        logger.info(
            f"Video conversion completed. {file} has been converted to {new_file_name}"
        )
        return new_file_name
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Video conversion FAILED. {file} has NOT been converted to {new_file_name}"
        )
        raise e


def copy_video_to_directory(file: str, video_directory: str) -> str:
    """Copies video file to video directory without conversion"""
    new_file_name = video_directory + file.split("/")[-1]
    logger = logging.getLogger("convert")
    if os.path.exists(new_file_name):
        return new_file_name
    try:
        # Construct the ffmpeg command
        subprocess.run(f"cp {file} {new_file_name}", shell=True)
        logger.info(f"Video copy completed. {file} has been copied to {new_file_name}")
        return new_file_name
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Video copy FAILED. {file} has NOT been copied to {new_file_name}"
        )
        raise e


def add_associated_video_files(
    nwb_file: NWBFile,
    metadata: dict,
    session_df: pd.DataFrame,
    video_directory: str,
    convert_video: bool = False,
):
    # make processing module for video files
    nwb_file.create_processing_module(
        name="video_files", description="Contains all associated video files data"
    )
    # make a behavioral Event object to hold videos
    video = BehavioralEvents(name="video")
    # add the video file data
    for video_metadata in metadata["associated_video_files"]:
        epoch = video_metadata["task_epochs"][0]
        # get the video file path
        video_path = None
        for file in session_df[session_df.file_extension == ".h264"].full_path:
            if video_metadata["name"].rsplit(".", 1)[0] in file:
                video_path = file
                break
        if video_path is None:
            raise FileNotFoundError(
                f"Could not find video file {video_metadata['name']} in session_df"
            )

        # get timestamps for this video
        # find the matching hw timestamps filepath
        video_index = video_path.split(".")[-2]
        video_hw_df = session_df.loc[
            np.logical_and(
                session_df.epoch == epoch,
                session_df.file_extension == ".cameraHWSync",
            )
        ]
        if not len(video_hw_df):
            raise ValueError(
                f"No cameraHWSync found for epoch {epoch}, video {video_index} in session_df"
            )
        video_timestamps_filepath = video_hw_df[
            [
                full_path.split(".")[-3] == video_index
                for full_path in video_hw_df.full_path
            ]
        ].full_path.to_list()[0]
        # get the timestamps
        video_timestamps = get_video_timestamps(video_timestamps_filepath)

        if convert_video:
            video_file_name = convert_h264_to_mp4(video_path, video_directory)
        else:
            video_file_name = copy_video_to_directory(video_path, video_directory)

        video.add_timeseries(
            ImageSeries(
                device=nwb_file.devices[
                    "camera_device " + str(video_metadata["camera_id"])
                ],
                name=video_metadata["name"],
                timestamps=video_timestamps,
                external_file=[video_file_name.split("/")[-1]],
                format="external",
                starting_frame=[0],
                description="video of animal behavior from epoch",
            )
        )
    if video_metadata is None:
        raise KeyError(f"Missing video metadata for epoch {epoch}")

    nwb_file.processing["video_files"].add(video)
    return
