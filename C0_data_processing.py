from C1_verification import verify
from C2_economic_functions import economics
import pandas as pd
import logging
import sys, shutil
import json
import numpy
import pandas as pd

from copy import deepcopy

class data_processing:
    def all(dict_values):
        ## Verify inputs
        # check whether input values can be true
        verify.check_input_values(dict_values)
        # Check, whether files (demand, generation) are existing
        helpers.evaluate_timeseries(dict_values, verify.lookup_file, 'verify')

        ## Complete data values
        # Receive data from timeseries and process their format
        helpers.evaluate_timeseries(dict_values, receive_data.timeseries_csv, 'receive_data')
        #todo add option to receive data online

        helpers.define_sink(dict_values, 'electricity_excess', None)
        # Add symbolic costs
        if 'electricity_storage' in dict_values:
            helpers.create_twins_in_out(dict_values['electricity_storage'], 'charge_controller', drop_symbolic_costs=True)
        if 'transformer_station' in dict_values:
            helpers.create_twins_in_out(dict_values, 'transformer_station', drop_symbolic_costs=True)
            helpers.define_source(dict_values, 'transformer_station', 'electricity_price_var_kWh')
            helpers.define_sink(dict_values, 'transformer_station', 'feedin_tariff')

        helpers.add_input_output_busses(dict_values)

        # Adds costs to each asset and sub-asset
        data_processing.economic_data(dict_values)

        data_processing.store_as_json(dict_values)
        return

    def economic_data(dict_values):
        # Calculate annuitiy factor
        dict_values['economic_data'].update({
            'annuity_factor': economics.annuity_factor(
                dict_values['economic_data']['project_duration'],
                dict_values['economic_data']['discount_factor'])})

        # Calculate crf
        dict_values['economic_data'].update({
            'crf': economics.crf(
                dict_values['economic_data']['project_duration'],
                dict_values['economic_data']['discount_factor'])})

        for asset_name in dict_values:
            # Main assets
            if 'lifetime' in dict_values[asset_name].keys():
                # Add lifetime capex (incl. replacement costs), calculate annuity (incl. om), and simulation annuity
                helpers.evaluate_lifetime_costs(dict_values['settings'],
                                                dict_values['economic_data'],
                                                dict_values[asset_name])

            # Sub-assets, ie. pv_installation and solar_inverter of PV plant
            for sub_asset_name in dict_values[asset_name]:
                if isinstance(dict_values[asset_name][sub_asset_name], dict):
                    if 'lifetime' in dict_values[asset_name][sub_asset_name].keys():
                        # Add lifetime capex (incl. replacement costs), calculate annuity (incl. om), and simulation annuity
                        helpers.evaluate_lifetime_costs(dict_values['settings'],
                                                        dict_values['economic_data'],
                                                        dict_values[asset_name][sub_asset_name])

                    for sub_sub_asset_name in dict_values[asset_name][sub_asset_name]:
                        if isinstance(dict_values[asset_name][sub_asset_name][sub_sub_asset_name], dict):
                            if 'lifetime' in dict_values[asset_name][sub_asset_name][sub_sub_asset_name].keys():
                                # Add lifetime capex (incl. replacement costs), calculate annuity (incl. om), and simulation annuity
                                helpers.evaluate_lifetime_costs(dict_values['simulation_settings'],
                                                                dict_values['economic_data'],
                                                                dict_values[asset_name][sub_asset_name][sub_sub_asset_name])


        logging.info('Processed cost data and added economic values.')
        return

    def store_as_json(dict_values):
        def convert(o):
            if isinstance(o, numpy.int64): return int(o)
            if isinstance(o, pd.DatetimeIndex): return "date_range"
            if isinstance(o, pd.datetime): return str(o)
            print(o)
            raise TypeError

        myfile = open(dict_values['user_input']['path_output_folder'] + '/json_input_processed.json', 'w')
        json_data = json.dumps(dict_values, skipkeys=True, sort_keys=True, default=convert, indent=4)
        myfile.write(json_data)
        myfile.close()
        print(myfile)
        return

class helpers:

    def create_twins_in_out(dict_asset, name_subasset, drop_symbolic_costs):
        subasset = dict_asset[name_subasset]
        subasset.update({'label': name_subasset + '_in'})
        subasset_symbolic = deepcopy(subasset)
        subasset_symbolic.update({'label': name_subasset + '_out'})
        if drop_symbolic_costs == True:
            for cost in ['capex', 'opex']:
                for suffix in ['_var', '_fix']:
                    subasset_symbolic.update({cost + suffix: 0})
        elif drop_symbolic_costs == False:
            pass
        else:
            logging.error('Coding error: drop_symbolic_costs has to be True/False.')

        del dict_asset[name_subasset]
        dict_asset.update({name_subasset: {'label': name_subasset+'_in_out',
                                           'in': subasset,
                                           'out': subasset_symbolic}})

        dict_asset[name_subasset]['in'].update({'type': 'transformer'})
        dict_asset[name_subasset]['out'].update({'type': 'transformer'})
        return

    def define_source(dict_values, asset_name, price_name):
        # todo not applicable jet for fuel source
        if price_name == None:
            price = 0
        elif price_name in dict_values[asset_name]['in'].keys():
            price = dict_values[asset_name]['in'][price_name]
        else:
            logging.warning('Price name %s does not exist in %s.', price_name, asset_name)

        dict_values[asset_name].update({'source': {'type': 'source',
                                                   'label': asset_name + '_source',
                                                   'price': price}})
        return

    def define_sink(dict_asset, asset_name, price_name):
        if price_name == None:
            dict_asset.update({asset_name: {'type': 'sink',
                                            'label': asset_name + '_sink',
                                            'price': 0}})

        elif asset_name in dict_asset.keys():
            if price_name in dict_asset[asset_name]['out'].keys():
                price = dict_asset[asset_name]['out'][price_name]
                if price_name == 'feedin_tariff':
                    price = -1 * price
                dict_asset[asset_name].update({'sink': {'type': 'sink',
                                                        'label': asset_name + '_sink',
                                                        'price': price}})
            else:
                logging.warning('Price name %s does not exist in %s.', price_name, asset_name)
        else:
            logging.error('Asset %s does not exist, while price_name = None.', asset_name)

        return

    def add_input_output_busses(dict_values):
        for asset in dict_values.keys():
            if asset in ['project_data', 'settings', 'economic_data', 'user_input', ]:
                pass
            elif asset == 'electricity_grid':
                logging.warning('%s has not been included in model jet, specifically efficiency.', asset)

            elif asset == 'electricity_excess':
                dict_values[asset].update({'input_bus_name': 'electricity'})

            elif asset == 'transformer_station':
                dict_values[asset]['in'].update({'input_bus_name': dict_values[asset]['in']['sector'] + '_utility_consumption',
                                                 'output_bus_name': dict_values[asset]['in']['sector']})
                dict_values[asset]['source'].update({'output_bus_name': dict_values[asset]['in']['sector'] + '_utility_consumption'})
                dict_values[asset]['out'].update({'input_bus_name': dict_values[asset]['out']['sector'],
                                                  'output_bus_name': dict_values[asset]['out']['sector'] + '_utility_feedin'})
                dict_values[asset]['sink'].update({'input_bus_name': dict_values[asset]['in']['sector'] + '_utility_feedin'})

            elif asset == 'pv_plant':
                dict_values[asset]['pv_installation'].update({'output_bus_name': 'electricity_dc_pv'})
                dict_values[asset]['solar_inverter'].update({'input_bus_name': 'electricity_dc_pv',
                                                             'output_bus_name': 'electricity'})
            elif asset == 'wind_plant':
                dict_values[asset]['wind_installation'].update({'output_bus_name': 'electricity'})

            elif asset == 'electricity_storage':
                dict_values[asset].update({'input_bus_name': 'electricity_dc_storage',
                                           'output_bus_name': 'electricity_dc_storage'})
                dict_values[asset]['charge_controller']['in'].update({'input_bus_name': 'electricity',
                                                              'output_bus_name': 'electricity_dc_storage'})
                dict_values[asset]['charge_controller']['out'].update({'input_bus_name': 'electricity_dc_storage',
                                                                       'output_bus_name': 'electricity'})

            elif asset == 'generator':
                dict_values[asset].update({'input_bus_name': 'Fuel',
                                   'output_bus_name': 'electricity'})

            elif asset == 'electricity_demand':
                for demand in dict_values[asset].keys():
                    if demand != 'label':
                        dict_values[asset][demand].update({'input_bus_name': 'electricity'})

            else:
                logging.warning('Asset %s undefined, no input/output busses added.', asset)

        return

    def evaluate_lifetime_costs(settings, economic_data, dict_asset):
        if 'capex_var' not in dict_asset:
            dict_asset.update({'capex_var': 0})

        dict_asset.update({'lifetime_capex_var':
                                       economics.capex_from_investment(dict_asset['capex_var'],
                                                                       dict_asset['lifetime'],
                                                                       economic_data['project_duration'],
                                                                       economic_data['discount_factor'],
                                                                       economic_data['tax'])})

        # Annuities of components including opex AND capex #
        dict_asset.update({'annuity_capex_opex_var':
                                       economics.annuity(dict_asset['lifetime_capex_var'],
                                                         economic_data['crf'])
                                       + dict_asset['opex_fix']})

        dict_asset.update({'lifetime_opex_fix':
                                       dict_asset['opex_fix'] * economic_data['annuity_factor']})

        dict_asset.update({'lifetime_opex_var':
                                       dict_asset['opex_var'] * economic_data['annuity_factor']})

        # Scaling annuity to timeframe
        # Updating all annuities above to annuities "for the timeframe", so that optimization is based on more adequate
        # costs. Includes project_cost_annuity, distribution_grid_cost_annuity, maingrid_extension_cost_annuity for
        # consistency eventhough these are not used in optimization.
        dict_asset.update({'simulation_annuity':
                                       dict_asset['annuity_capex_opex_var'] / 365
                                       * settings['evaluated_period']})

        return

    def evaluate_timeseries(dict_values, function, use):
        input_folder = dict_values['simulation_settings']['path_input_folder']
        # Accessing timeseries of components
        for asset_name in ['pv_plant', 'wind_plant']:
            if asset_name in dict_values:
                if asset_name == 'pv_plant':
                    sub_name = 'pv_installation'
                elif asset_name == 'wind_plant':
                    sub_name = 'wind_installation'
                file_path = input_folder + dict_values[asset_name][sub_name]['file_name']
                # Check if file existent
                if use == 'verify':
                    # check if specific demand timeseries exists
                    function(file_path, asset_name)
                elif use == 'receive_data':
                    # receive data and write it into dict_values
                    function(dict_values['settings'], dict_values['user_input'], dict_values[asset_name][sub_name], file_path, asset_name)

        # Accessing timeseries of demands
        for demand_type in ['electricity_demand', 'heat_demand']:
            if demand_type in dict_values:
                # Check for each
                for demand_key in dict_values['electricity_demand']:
                    if demand_key != 'label':
                        file_path = input_folder + dict_values[demand_type][demand_key]['file_name']
                        if use == 'verify':
                            # check if specific demand timeseries exists
                            function(file_path, demand_key)
                        elif use == 'receive_data':
                            # receive data and write it into dict_values
                            function(dict_values['settings'], dict_values['user_input'], dict_values[demand_type][demand_key], file_path, demand_key)
        return

class receive_data:
    def timeseries_csv(settings, user_input, dict_asset, file_path, name):
        data_set = pd.read_csv(file_path, sep=';')
        if len(data_set.index) == settings['periods']:
            dict_asset.update({'timeseries': pd.Series(data_set['kW'].values, index = settings['index'])})
            logging.debug('Added timeseries of %s (%s).', name, file_path)
        elif len(data_set.index) >= settings['periods']:
            dict_asset.update({'timeseries': pd.Series(data_set['kW'][0:len(settings['index'])].values,
                                                          index=settings['index'])})
            logging.info('Provided timeseries of %s (%s) longer than evaluated period. '
                         'Excess data dropped.', name, file_path)

        elif len(data_set.index) <= settings['periods']:
            logging.critical('Input errror! '
                             'Provided timeseries of %s (%s) shorter then evaluated period. '
                             'Operation terminated', name, file_path)
            sys.exit()

        dict_asset.update({'timeseries_peak': max(dict_asset['timeseries']),
                           'timeseries_total': sum(dict_asset['timeseries']),
                           'timeseries_average': sum(dict_asset['timeseries'])/len(dict_asset['timeseries'])})

        shutil.copy(file_path, user_input['path_output_folder_inputs']+dict_asset['file_name'])
        logging.debug('Copied timeseries %s to output folder / inputs.', file_path)
        return

    #get timeseries from online source
    def timeseries_online():
        return
