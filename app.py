from pathlib import Path
import shutil
import subprocess
import json
import uuid
import logging

from flask import Flask, request, jsonify
import esoreader
import pandas as pd


app = Flask(__name__)


if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)


# Base path to be mounted to external access
BASE_PATH = Path("/var/simdata/energyplus")


# shared between workers
IDD_ADDRESS = Path("/var/simdata/data/eplus.idd")  # TODO use original
VARS_ADDRESS = BASE_PATH / "varnames.json"


@app.route("/setup", methods=['POST'])
def setup():

    # maybe binary object (ax_text=False)
    content_type = request.args.get('type')  # epw or idd or 'vars'

    if content_type == 'epw':
        # name of the calculation setup
        setup_name = request.args.get('name')
        app.logger.debug('Setting up epw for: {n}'.format(n=setup_name))
        if setup_name is None:
            return "Please provide 'name' argument if setting up epw"

        setup_path = BASE_PATH / setup_name

        if not setup_path.exists():
            setup_path.mkdir(parents=True)

        epw_path = setup_path / "weather.epw"

        content = request.get_data(as_text=True)  # maybe as_text=False?
        # change windows type newline characters to unix type
        content = content.replace('\r\n', '\n')
        with epw_path.open('w') as epw_file:
            epw_file.write(content)

    elif content_type == 'idd':
        app.logger.debug('Setting up IDD')
        content = request.get_data(as_text=True)  # maybe as_text=False?
        # change windows type newline characters to unix type
        content = content.replace('\r\n', '\n')
        with IDD_ADDRESS.open('w') as idd_file:
            idd_file.write(content)

    elif content_type == 'vars':
        app.logger.debug('Setting up VARS')
        content = request.get_json()
        with VARS_ADDRESS.open('w') as vars_json:
            json.dump(content, vars_json, indent=4)

    else:
        return "Type options are following: 'epw', 'idd' or 'vars"

    return "OK"


@app.route("/check", methods=['GET'])
def check():
    setup_name = request.args.get('name')
    app.logger.debug('Checking setup for {n}'.format(n=setup_name))
    setup_path = BASE_PATH / setup_name
    epw_path = setup_path / "weather.epw"

    if IDD_ADDRESS.exists():
        if epw_path.exists():
            return "OK"
        else:
            return "EPW file is missing"
    else:
        if epw_path.exists():
            # return "IDD file is missing"
            return "OK"
        else:
            # return "EPW and IDD file is missing"
            return "EPW file is missing"


@app.route("/run", methods=['POST'])
def run():
    setup_name = request.args.get('name')
    setup_path = BASE_PATH / setup_name
    epw_path = setup_path / "weather.epw"

    simulation_id = request.args.get('id')
    if simulation_id is None:
        simulation_id = str(uuid.uuid1())
    app.logger.info('Running simulation for {n} with id: {id}'.format(
        n=setup_name, id=simulation_id))
    # Different for each run
    simulation_address = setup_path / "run" / simulation_id
    if not simulation_address.exists():
        simulation_address.mkdir(parents=True)

    idf_address = simulation_address / "model.idf"

    idf = request.get_data(as_text=True)

    # change windows type newline characters to unix type
    idf = idf.replace('\r\n', '\n')
    with idf_address.open('w') as idf_file:
        idf_file.write(idf)

    # compose Energy Plus command
    cmd = ["energyplus"]
    cmd += ["-d", str(simulation_address)]  # output folder
    cmd += ["-w", str(epw_path)]  # weather file
    # cmd += ["-i", IDD_ADDRESS]  # input data dictionary
    cmd += [str(idf_address)]  # idf input file

    if gunicorn_logger.level > 15:
        # info - do not print put energyplus command line output
        subprocess.run(cmd,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    else:  # debug
        subprocess.run(cmd)
    return simulation_id


@app.route("/results", methods=['GET'])
def results():
    setup_name = request.args.get('name')
    setup_path = BASE_PATH / setup_name

    variables = request.args.getlist('variables')  # list
    simulation_id = request.args.get('id')
    typ = request.args.get('type')
    period = request.args.get('period')

    debug_message = 'Reading results for {n} with id: {id}'.format(
        n=setup_name, id=simulation_id)
    debug_message += '; type: {t}; period: {p}, variables: {v}'.format(
        t=typ, p=period, v=variables)
    app.logger.debug(debug_message)

    simulation_address = setup_path / "run" / simulation_id
    if not simulation_address.exists():
        message = 'No result directory for id: {i}'.format(i=simulation_id)
        app.logger.debug(message)
        return message

    end_path = simulation_address / 'eplusout.end'
    with end_path.open('r') as end_file:
        end_success = end_file.readline()
        app.logger.debug(end_success)
    if 'EnergyPlus Completed Successfully' not in end_success:
        message = 'Simulation failed for id: {i}'.format(i=simulation_id)
        app.logger.info(message)
        return message

    eso_path = simulation_address / 'eplusout.eso'
    if not eso_path.exists():
        message = 'No result for id: {i}'.format(i=simulation_id)
        app.logger.debug(message)
        return message

    eso = esoreader.read_from_path(str(eso_path))

    with VARS_ADDRESS.open('r') as var_data:
        var_info = json.load(var_data)
        var_dict = var_info['var_dict']
        units = var_info['units']

    res_dfs = []
    # app.logger.debug(variables)
    for var in variables:
        var_name = var_dict[typ][var]
        df = eso.to_frame(var_name, frequency=period)
        df = df.sum(axis='columns')
        df.name = var
        if units[var] == 'J':  # Convert to kWh
            df /= (3.6 * 1e6)
        elif units[var] == '-J':
            df /= -(3.6 * 1e6)
        res_dfs.append(df)

    result = pd.concat(res_dfs, axis='columns')

    return jsonify(result.to_json(orient='split'))


@app.route("/results/detailed", methods=['GET'])
def results_detailed():
    setup_name = request.args.get('name')
    setup_path = BASE_PATH / setup_name

    variable = request.args.get('variable')
    simulation_id = request.args.get('id')
    typ = request.args.get('type')
    period = request.args.get('period')

    simulation_address = setup_path / "run" / simulation_id
    end_path = simulation_address / 'eplusout.end'
    with end_path.open('r') as end_file:
        end_success = end_file.readline()
    if 'EnergyPlus Completed Successfully' not in end_success:
        message = 'Simulation failed for id: {i}'.format(i=simulation_id)
        app.logger.debug(message)
        return message
    eso_path = simulation_address / 'eplusout.eso'
    if not eso_path.exists():
        message = 'No result for id: {i}'.format(i=simulation_id)
        app.logger.debug(message)
        return message

    eso = esoreader.read_from_path(str(eso_path))

    with VARS_ADDRESS.open('r') as var_data:
        var_info = json.load(var_data)
        var_dict = var_info['var_dict']
        units = var_info['units']

    var_name = var_dict[typ][variable]
    df = eso.to_frame(var_name, frequency=period)
    if units[variable] == 'J':  # Convert to kWh
        df /= (3.6 * 1e6)
    elif units[variable] == '-J':
        df /= -(3.6 * 1e6)

    return jsonify(df.to_json(orient='split'))


@app.route("/cleanup", methods=['GET'])
def clean_up():
    setup_name = request.args.get('name')
    app.logger.info('Cleaning up simulation results for {n}'.format(
        n=setup_name))
    setup_path = BASE_PATH / setup_name

    folder = setup_path / "run"
    for filename in folder.iterdir():
        try:
            if filename.is_file() or filename.is_symlink():
                filename.unlink()
            elif filename.is_dir():
                shutil.rmtree(str(filename))
        except Exception as exc:
            app.logger.error(exc)
            return 'Failed to delete {fp}. Reason: {e}'.format(
                fp=filename, e=exc)
    app.logger.info('Simulation results deleted')
    return 'OK'


@app.route("/cleanup/result", methods=['GET'])
def drop_result():
    setup_name = request.args.get('name')
    setup_path = BASE_PATH / setup_name

    simulation_id = request.args.get('id')

    debug_message = 'Deleting results for {n} with id: {id}'.format(
        n=setup_name, id=simulation_id)
    app.logger.debug(debug_message)

    simulation_address = setup_path / "run" / simulation_id

    try:
        if simulation_address.is_dir():
            shutil.rmtree(str(simulation_address))
    except Exception as exc:
        app.logger.error(exc)
        return 'Failed to delete {fp}. Reason: {e}'.format(
            fp=simulation_address, e=exc)
    app.logger.debug('Simulation results deleted for id: '.format(
        id=simulation_id))
    return 'OK'


@app.route("/download", methods=['GET'])
def download():

    content_type = request.args.get('type')  # epw or idd or 'vars'

    if content_type == 'epw':
        # name of the calculation setup
        setup_name = request.args.get('name')
        if setup_name is None:
            return "Please provide 'name' argument if setting up epw"

        setup_path = BASE_PATH / setup_name

        epw_path = setup_path / "weather.epw"

        if not epw_path.exists():
            return "No epw file found for setup: {n}".format(n=setup_name)

        with epw_path.open('r') as epw_file:
            content = epw_file.read()
        return content

    elif content_type == 'idd':
        with IDD_ADDRESS.open('r') as idd_file:
            content = idd_file.read()
        return content

    elif content_type == 'vars':
        with VARS_ADDRESS.open('r') as vars_json:
            content = json.load(vars_json)
        return jsonify(content)

    else:
        return "Type options are following: 'epw', 'idd' or 'vars"


# use only for development:
if __name__ == '__main__':
    app.run(debug=True, port=9090, host='0.0.0.0')
