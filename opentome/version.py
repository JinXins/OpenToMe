# Copyright (c) CAIRI AI Lab. All rights reserved

__version__ = '0.0.1'


def parse_version_info(version_str):
    """Parse a version string into a tuple.

    Args:
        version_str (str): The version string.
    Returns:
        tuple[int | str]: The version info, e.g., "0.0.1" is parsed into
            (0, 1, 0), and "2.0.0rcx" is parsed into (2, 0, 0, 'rcx').
    """
    version_info = []
    for x in version_str.split('.'):
        if x.isdigit():
            version_info.append(int(x))
        elif x.find('rc') != -1:
            patch_version = x.split('rc')
            version_info.append(int(patch_version[0]))
            version_info.append(f'rc{patch_version[1]}')
    return tuple(version_info)


version_info = parse_version_info(__version__)

__all__ = ['__version__', 'version_info', 'parse_version_info']
