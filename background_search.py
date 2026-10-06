"""Run the regex engine outside the GUI process so pathological patterns cannot stall Tk."""

import multiprocessing


def search_worker(path, pattern, ignore_case, output, include_locators=False):
    from db_browser import connect_database, search_database, RESULT_LIMIT
    connection = None
    try:
        connection = connect_database(path)
        matches, total = [], 0
        locators = []
        def capture(locator):
            if len(locators) < RESULT_LIMIT:
                locators.append(locator)
        for match in search_database(connection, pattern, ignore_case,
                                     progress=lambda name: output.send(('progress', f'Searching {name}…')),
                                     capture=capture if include_locators else None):
            total += 1
            if len(matches) < RESULT_LIMIT:
                matches.append(match)
        output.send(('done', (matches, total, locators) if include_locators else (matches, total)))
    except Exception as error:
        output.send(('error', str(error)))
    finally:
        if connection:
            connection.close()
        output.close()


def run_search_process(path, pattern, ignore_case, cancel, progress=None, include_locators=False):
    context = multiprocessing.get_context('spawn')
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=search_worker, args=(path, pattern, ignore_case, outgoing, include_locators), daemon=True)
    try:
        if cancel.is_set():
            raise RuntimeError('Operation cancelled.')
        process.start()
        outgoing.close()
        while True:
            if cancel.is_set():
                raise RuntimeError('Operation cancelled.')
            if incoming.poll(0.1):
                try:
                    kind, payload = incoming.recv()
                except EOFError:
                    raise RuntimeError('Search process exited without returning results.') from None
                if kind == 'done':
                    return payload
                if kind == 'error':
                    raise RuntimeError(payload)
                if progress:
                    progress(payload)
            elif not process.is_alive():
                raise RuntimeError('Search process exited without returning results.')
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join()
            process.close()
        incoming.close()
        outgoing.close()
