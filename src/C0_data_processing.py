import logging
import os
import shutil
import sys

import matplotlib.pyplot as plt
import pandas as pd

logging.getLogger("matplotlib.font_manager").disabled = True

from src.constants import (
    INPUTS_COPY,
    TIME_SERIES,
    PATHS_TO_PLOTS,
    PLOTS_DEMANDS,
    PLOTS_RESOURCES,
    SIMULATION_SETTINGS,
)

from src.constants_json_strings import (
    UNIT,
    VALUE,
    ENERGY_CONVERSION,
    ENERGY_CONSUMPTION,
    ENERGY_PRODUCTION,
    ENERGY_STORAGE,
    ENERGY_BUSSES,
    ENERGY_PROVIDERS,
    OEMOF_SOURCE,
    OEMOF_SINK,
    OEMOF_ASSET_TYPE,
    PROJECT_DURATION,
    DISCOUNTFACTOR,
    TAX,
    LABEL,
)


import src.C1_verification as verify
import src.C2_economic_functions as economics
import src.F0_output as output

"""
Module C0 prepares the data red from csv or json for simulation, ie. pre-processes it. 
- Verify input values with C1
- Identify energyVectors and write them to project_data/sectors
- Create a sink for each energyVector (this actually might be changed in the future - create an excess sink for each bus?)
- Process start_date/simulation_duration to pd.datatimeindex (future: Also consider timesteplenghts)
- Add economic parameters to json with C2
- Calculate "simulation annuity" used in oemof model
- Add demand sinks to energyVectors (this should actually be changed and demand sinks should be added to bus relative to input_direction, also see issue #179)
- Translate input_directions/output_directions to bus names
- Add missing cost data to automatically generated objects (eg. DSO transformers)
- Read timeseries of assets and store into json (differ between one-column csv, multi-column csv)
- Read timeseries for parameter of an asset, eg. efficiency
- Parse list of inputs/outputs, eg. for chp
- Define dso sinks, soures, transformer stations (this will be changed due to bug #119), also for peak demand pricing
- Add a source if a conversion object is connected to a new input_direction (bug #186)
- Define all necessary energyBusses and add all assets that are connected to them specifically with asset name and label
"""


class PeakDemandPricingPeriodsOnlyForYear(ValueError):
    # Exception raised when there is a number of peak demand pricing periods considered while no year is simulated.
    pass


def all(dict_values):
    """
    Function executing all pre-processing steps necessary
    :param dict_values
    All input data in dict format

    :return Pre-processed dictionary with all input parameters

    """
    simulation_settings(dict_values[SIMULATION_SETTINGS])
    economic_parameters(dict_values["economic_data"])
    identify_energy_vectors(dict_values)

    ## Verify inputs
    # todo check whether input values can be true
    # verify.check_input_values(dict_values)
    # todo Check, whether files (demand, generation) are existing

    # Adds costs to each asset and sub-asset
    process_all_assets(dict_values)

    output.store_as_json(
        dict_values,
        dict_values[SIMULATION_SETTINGS]["path_output_folder"],
        "json_input_processed",
    )
    return


def identify_energy_vectors(dict_values):
    """
    Identifies all energyVectors used in the energy system by checking every entry 'energyVector' of all assets.
    energyVectors later will be used to distribute costs and KPI amongst the sectors (not implemented)

    :param: dict
    All input data in dict format

    :return:
    Update dict['project_data'] by used sectors
    """
    dict_of_sectors = {}
    names_of_sectors = ""
    for level1 in dict_values.keys():
        for level2 in dict_values[level1].keys():
            if (
                isinstance(dict_values[level1][level2], dict)
                and "energyVector" in dict_values[level1][level2].keys()
            ):
                energy_vector_name = dict_values[level1][level2]["energyVector"]
                if energy_vector_name not in dict_of_sectors.keys():
                    dict_of_sectors.update(
                        {energy_vector_name: energy_vector_name.replace("_", " ")}
                    )
                    names_of_sectors = names_of_sectors + energy_vector_name + ", "

    dict_values["project_data"].update({"sectors": dict_of_sectors})
    logging.info(
        "The energy system modelled includes following energy vectors / sectors: %s",
        names_of_sectors[:-2],
    )
    return


def simulation_settings(simulation_settings):
    """
    Updates simulation settings by all time-related parameters.
    :param: dict
    Simulation parameters of the input data
    :return: dict
    Update simulation_settings by start date, end date, timeindex, and number of simulation periods
    """
    simulation_settings.update(
        {"start_date": pd.to_datetime(simulation_settings["start_date"])}
    )
    simulation_settings.update(
        {
            "end_date": simulation_settings["start_date"]
            + pd.DateOffset(
                days=simulation_settings["evaluated_period"][VALUE], hours=-1
            )
        }
    )
    # create time index used for initializing oemof simulation
    simulation_settings.update(
        {
            "time_index": pd.date_range(
                start=simulation_settings["start_date"],
                end=simulation_settings["end_date"],
                freq=str(simulation_settings["timestep"][VALUE]) + "min",
            )
        }
    )

    simulation_settings.update({"periods": len(simulation_settings["time_index"])})
    return simulation_settings


def economic_parameters(economic_parameters):
    """Calculate annuity factor

    :param economic_parameters:
    :return:
    """

    economic_parameters.update(
        {
            "annuity_factor": {
                "value": economics.annuity_factor(
                    economic_parameters[PROJECT_DURATION]["value"],
                    economic_parameters[DISCOUNTFACTOR]["value"],
                ),
                UNIT: "?",
            }
        }
    )
    # Calculate crf
    economic_parameters.update(
        {
            "crf": {
                "value": economics.crf(
                    economic_parameters[PROJECT_DURATION]["value"],
                    economic_parameters[DISCOUNTFACTOR]["value"],
                ),
                UNIT: "?",
            }
        }
    )
    return


def process_all_assets(dict_values):
    """defines dict_values['energyBusses'] for later reference

    :param dict_values:
    :return:
    """
    #
    define_busses(dict_values)

    # Define all excess sinks for sectors
    for sector in dict_values["project_data"]["sectors"]:
        define_sink(
            dict_values,
            dict_values["project_data"]["sectors"][sector] + " excess",
            {"value": 0, UNIT: "currency/kWh"},
            dict_values["project_data"]["sectors"][sector],
        )
        logging.debug(
            "Created excess sink for sector %s",
            dict_values["project_data"]["sectors"][sector],
        )

    # process all energyAssets:
    # Attention! Order of asset_groups important. for energyProviders/energyConversion sinks and sources
    # might be defined that have to be processed in energyProduction/energyConsumption
    asset_group_list = {
        ENERGY_PROVIDERS: energyProviders,
        ENERGY_CONVERSION: energyConversion,
        ENERGY_STORAGE: energyStorage,
        ENERGY_PRODUCTION: energyProduction,
        ENERGY_CONSUMPTION: energyConsumption,
    }

    for asset_group, asset_function in asset_group_list.items():
        logging.info("Pre-processing all assets in asset group %s.", asset_group)
        if asset_group != ENERGY_PROVIDERS:
            # Populates dict_values['energyBusses'] with assets
            update_busses_in_out_direction(dict_values, dict_values[asset_group])

        asset_function(dict_values, asset_group)

        logging.debug(
            "Finished pre-processing all assets in asset group %s.", asset_group
        )

    logging.info("Processed cost data and added economic values.")
    return


def energyConversion(dict_values, group):
    """Add lifetime capex (incl. replacement costs), calculate annuity (incl. om), and simulation annuity to each asset

    :param dict_values:
    :param group:
    :return:
    """
    #
    for asset in dict_values[group]:
        define_missing_cost_data(dict_values, dict_values[group][asset])
        evaluate_lifetime_costs(
            dict_values[SIMULATION_SETTINGS],
            dict_values["economic_data"],
            dict_values[group][asset],
        )
        # check if maximumCap exists and add it to dict_values
        add_maximum_cap(dict_values=dict_values, group=group, asset=asset)

        # in case there is only one parameter provided (input bus and one output bus)
        if isinstance(dict_values[group][asset]["efficiency"]["value"], dict):
            receive_timeseries_from_csv(
                dict_values,
                dict_values[SIMULATION_SETTINGS],
                dict_values[group][asset],
                "efficiency",
            )
        # in case there is more than one parameter provided (either (A) n input busses and 1 output bus or (B) 1 input bus and n output busses)
        # dictionaries with filenames and headers will be replaced by timeseries, scalars will be mantained
        elif isinstance(dict_values[group][asset]["efficiency"]["value"], list):
            treat_multiple_flows(dict_values[group][asset], dict_values, "efficiency")

            # same distinction of values provided with dictionaries (one input and one output) or list (multiple).
            # They can at turn be scalars, mantained, or timeseries
            logging.debug(
                "Asset %s has multiple input/output busses with a list of efficiencies. Reading list",
                dict_values[group][asset][LABEL],
            )
    return


def energyProduction(dict_values, group):
    """

    :param dict_values:
    :param group:
    :return:
    """
    for asset in dict_values[group]:
        define_missing_cost_data(dict_values, dict_values[group][asset])
        evaluate_lifetime_costs(
            dict_values[SIMULATION_SETTINGS],
            dict_values["economic_data"],
            dict_values[group][asset],
        )

        if "file_name" in dict_values[group][asset]:
            receive_timeseries_from_csv(
                dict_values,
                dict_values[SIMULATION_SETTINGS],
                dict_values[group][asset],
                "input",
            )
        # check if maximumCap exists and add it to dict_values
        add_maximum_cap(dict_values, group, asset)

    return


def energyStorage(dict_values, group):
    """

    :param dict_values:
    :param group:
    :return:
    """
    for asset in dict_values[group]:
        for subasset in ["storage capacity", "input power", "output power"]:
            define_missing_cost_data(
                dict_values, dict_values[group][asset][subasset],
            )
            evaluate_lifetime_costs(
                dict_values[SIMULATION_SETTINGS],
                dict_values["economic_data"],
                dict_values[group][asset][subasset],
            )

            # check if parameters are provided as timeseries
            for parameter in ["efficiency", "soc_min", "soc_max"]:
                if parameter in dict_values[group][asset][subasset] and isinstance(
                    dict_values[group][asset][subasset][parameter]["value"], dict
                ):
                    receive_timeseries_from_csv(
                        dict_values,
                        dict_values[SIMULATION_SETTINGS],
                        dict_values[group][asset][subasset],
                        parameter,
                    )
                elif parameter in dict_values[group][asset][subasset] and isinstance(
                    dict_values[group][asset][subasset][parameter]["value"], list
                ):
                    treat_multiple_flows(
                        dict_values[group][asset][subasset], dict_values, parameter
                    )
            # check if maximumCap exists and add it to dict_values
            add_maximum_cap(dict_values, group, asset, subasset)

        # define input and output bus names
        dict_values[group][asset].update(
            {
                "input_bus_name": bus_suffix(
                    dict_values[group][asset]["inflow_direction"]
                )
            }
        )
        dict_values[group][asset].update(
            {
                "output_bus_name": bus_suffix(
                    dict_values[group][asset]["outflow_direction"]
                )
            }
        )
    return


def energyProviders(dict_values, group):
    """

    :param dict_values:
    :param group:
    :return:
    """
    # add sources and sinks depending on items in energy providers as pre-processing
    for asset in dict_values[group]:
        define_dso_sinks_and_sources(dict_values, asset)

        # Add lifetime capex (incl. replacement costs), calculate annuity
        # (incl. om), and simulation annuity to each asset
        define_missing_cost_data(dict_values, dict_values[group][asset])
        evaluate_lifetime_costs(
            dict_values[SIMULATION_SETTINGS],
            dict_values["economic_data"],
            dict_values[group][asset],
        )
    return


def energyConsumption(dict_values, group):
    """

    :param dict_values:
    :param group:
    :return:
    """
    for asset in dict_values[group]:
        define_missing_cost_data(dict_values, dict_values[group][asset])
        evaluate_lifetime_costs(
            dict_values[SIMULATION_SETTINGS],
            dict_values["economic_data"],
            dict_values[group][asset],
        )
        if "input_bus_name" not in dict_values[group][asset]:
            dict_values[group][asset].update(
                {
                    "input_bus_name": bus_suffix(
                        dict_values[group][asset]["energyVector"]
                    )
                }
            )

        if "file_name" in dict_values[group][asset]:
            receive_timeseries_from_csv(
                dict_values,
                dict_values[SIMULATION_SETTINGS],
                dict_values[group][asset],
                "input",
                is_demand_profile=True,
            )
    return


def define_missing_cost_data(dict_values, dict_asset):
    """

    :param dict_values:
    :param dict_asset:
    :return:
    """

    # read timeseries with filename provided for variable costs.
    # if multiple opex_var are given for multiple busses, it checks if any v
    # alue is a timeseries
    if "opex_var" in dict_asset:
        if isinstance(dict_asset["opex_var"]["value"], dict):
            receive_timeseries_from_csv(
                dict_values, dict_values[SIMULATION_SETTINGS], dict_asset, "opex_var"
            )
        elif isinstance(dict_asset["opex_var"]["value"], list):
            treat_multiple_flows(dict_asset, dict_values, "opex_var")

    economic_data = dict_values["economic_data"]

    basic_costs = {
        "optimizeCap": {"value": False, UNIT: "bool"},
        UNIT: "?",
        "installedCap": {"value": 0.0, UNIT: UNIT},
        "capex_fix": {"value": 0, UNIT: "currency"},
        "capex_var": {"value": 0, UNIT: "currency/unit"},
        "opex_fix": {"value": 0, UNIT: "currency/year"},
        "opex_var": {"value": 0, UNIT: "currency/unit/year"},
        "lifetime": {"value": economic_data[PROJECT_DURATION]["value"], UNIT: "year",},
    }

    # checks that an asset has all cost parameters needed for evaluation.
    # Adds standard values.
    str = ""
    for cost in basic_costs:
        if cost not in dict_asset:
            dict_asset.update({cost: basic_costs[cost]})
            str = str + " " + cost

    if len(str) > 1:
        logging.debug("Added basic costs to asset %s: %s", dict_asset[LABEL], str)
    return


def define_busses(dict_values):
    """

    :param dict_values:
    :return:
    """
    # create new group of assets: busses
    dict_values.update({ENERGY_BUSSES: {}})

    # defines energy busses of sectors
    for sector in dict_values["project_data"]["sectors"]:
        dict_values[ENERGY_BUSSES].update(
            {bus_suffix(dict_values["project_data"]["sectors"][sector]): {}}
        )
    # defines busses accessed by conversion assets
    update_busses_in_out_direction(dict_values, dict_values[ENERGY_CONVERSION])
    return


def update_busses_in_out_direction(dict_values, asset_group, **kwargs):
    """

    :param dict_values:
    :param asset_group:
    :param kwargs:
    :return:
    """
    # checks for all assets of an group
    for asset in asset_group:
        # the bus that is connected to the inflow
        if "inflow_direction" in asset_group[asset]:
            bus = asset_group[asset]["inflow_direction"]
            if isinstance(bus, list):
                bus_list = []
                for subbus in bus:
                    bus_list.append(bus_suffix(subbus))
                    update_bus(dict_values, subbus, asset, asset_group[asset][LABEL])
                asset_group[asset].update({"input_bus_name": bus_list})
            else:
                asset_group[asset].update({"input_bus_name": bus_suffix(bus)})
                update_bus(dict_values, bus, asset, asset_group[asset][LABEL])
        # the bus that is connected to the outflow
        if "outflow_direction" in asset_group[asset]:
            bus = asset_group[asset]["outflow_direction"]
            if isinstance(bus, list):
                bus_list = []
                for subbus in bus:
                    bus_list.append(bus_suffix(subbus))
                    update_bus(dict_values, subbus, asset, asset_group[asset][LABEL])
                asset_group[asset].update({"output_bus_name": bus_list})
            else:
                asset_group[asset].update({"output_bus_name": bus_suffix(bus)})
                update_bus(dict_values, bus, asset, asset_group[asset][LABEL])

    return


def bus_suffix(bus):
    """

    :param bus:
    :return:
    """
    bus_label = bus + " bus"
    return bus_label


def update_bus(dict_values, bus, asset, asset_label):
    """

    :param dict_values:
    :param bus:
    :param asset:
    :param asset_label:
    :return:
    """
    bus_label = bus_suffix(bus)
    if bus_label not in dict_values[ENERGY_BUSSES]:
        # add bus to asset group energyBusses
        dict_values[ENERGY_BUSSES].update({bus_label: {}})

    # Asset should added to respective bus
    dict_values[ENERGY_BUSSES][bus_label].update({asset: asset_label})
    logging.debug("Added asset %s to bus %s", asset_label, bus_label)
    return


def define_dso_sinks_and_sources(dict_values, dso):
    """

    :param dict_values:
    :param dso:
    :return:
    """
    # define to shorten code
    number_of_pricing_periods = dict_values[ENERGY_PROVIDERS][dso][
        "peak_demand_pricing_period"
    ]["value"]

    # check number of pricing periods - if >1 the simulation has to cover a whole year!
    if number_of_pricing_periods > 1:
        if dict_values[SIMULATION_SETTINGS]["evaluated_period"]["value"] != 365:
            raise PeakDemandPricingPeriodsOnlyForYear(
                f"For taking peak demand pricing periods > 1 into account,"
                f"the evaluation period has to be 365 days."
                f"\n Message for dev: This is not technically true, "
                f"as the evaluation period has to approximately be "
                f"larger than 365/peak demand pricing periods (see #331)."
            )

    # defines the evaluation period
    months_in_a_period = 12 / number_of_pricing_periods
    logging.info(
        "Peak demand pricing is taking place %s times per year, ie. every %s "
        "months.",
        number_of_pricing_periods,
        months_in_a_period,
    )

    dict_asset = dict_values[ENERGY_PROVIDERS][dso]
    if isinstance(dict_asset["peak_demand_pricing"]["value"], dict):
        receive_timeseries_from_csv(
            dict_values,
            dict_values[SIMULATION_SETTINGS],
            dict_asset,
            "peak_demand_pricing",
        )

    peak_demand_pricing = dict_values[ENERGY_PROVIDERS][dso]["peak_demand_pricing"][
        "value"
    ]
    if isinstance(peak_demand_pricing, float) or isinstance(peak_demand_pricing, int):
        logging.debug(
            "The peak demand pricing price of %s %s is set as capex_var of "
            "the sources of grid energy.",
            peak_demand_pricing,
            dict_values["economic_data"]["currency"],
        )
    else:
        logging.debug(
            "The peak demand pricing price of %s %s is set as capex_var of "
            "the sources of grid energy.",
            sum(peak_demand_pricing) / len(peak_demand_pricing),
            dict_values["economic_data"]["currency"],
        )

    peak_demand_pricing = {
        "value": dict_values[ENERGY_PROVIDERS][dso]["peak_demand_pricing"]["value"],
        UNIT: "currency/kWpeak",
    }

    list_of_dso_energyProduction_assets = []
    if number_of_pricing_periods == 1:
        # if only one period: avoid suffix dso+'_consumption_period_1"
        timeseries = pd.Series(1, index=dict_values[SIMULATION_SETTINGS]["time_index"])
        define_source(
            dict_values,
            dso + "_consumption",
            dict_values[ENERGY_PROVIDERS][dso]["energy_price"],
            dict_values[ENERGY_PROVIDERS][dso]["outflow_direction"],
            timeseries,
            opex_fix=peak_demand_pricing,
        )
        list_of_dso_energyProduction_assets.append(dso + "_consumption")
    else:
        # define one source for each pricing period
        for pricing_period in range(1, number_of_pricing_periods + 1):
            timeseries = pd.Series(
                0, index=dict_values[SIMULATION_SETTINGS]["time_index"]
            )
            time_period = pd.date_range(
                start=dict_values[SIMULATION_SETTINGS]["start_date"]
                + pd.DateOffset(months=(pricing_period - 1) * months_in_a_period),
                end=dict_values[SIMULATION_SETTINGS]["start_date"]
                + pd.DateOffset(months=pricing_period * months_in_a_period, hours=-1),
                freq=str(dict_values[SIMULATION_SETTINGS]["timestep"]["value"]) + "min",
            )

            timeseries = timeseries.add(pd.Series(1, index=time_period), fill_value=0)
            dso_source_name = dso + "_consumption_period_" + str(pricing_period)
            define_source(
                dict_values,
                dso_source_name,
                dict_values[ENERGY_PROVIDERS][dso]["energy_price"],
                dict_values[ENERGY_PROVIDERS][dso]["outflow_direction"],
                timeseries,
                opex_fix=peak_demand_pricing,
            )
            list_of_dso_energyProduction_assets.append(dso_source_name)

    define_sink(
        dict_values,
        dso + "_feedin",
        dict_values[ENERGY_PROVIDERS][dso]["feedin_tariff"],
        dict_values[ENERGY_PROVIDERS][dso]["inflow_direction"],
        capex_var={"value": 0, UNIT: "currency/kW"},
    )

    dict_values[ENERGY_PROVIDERS][dso].update(
        {
            "connected_consumption_sources": list_of_dso_energyProduction_assets,
            "connected_feedin_sink": dso + "_feedin",
        }
    )

    return


def define_source(dict_values, asset_name, price, output_bus, timeseries, **kwargs):
    """

    :param dict_values:
    :param asset_name:
    :param price:
    :param output_bus:
    :param timeseries:
    :param kwargs:
    :return:
    """
    # create name of bus. Check if multiple busses are given
    if isinstance(output_bus, list):
        output_bus_name = []
        for bus in output_bus:
            output_bus_name.append(bus_suffix(bus))
    else:
        output_bus_name = bus_suffix(output_bus)

    source = {
        OEMOF_ASSET_TYPE: OEMOF_SOURCE,
        LABEL: asset_name + " source",
        "output_direction": output_bus,
        "output_bus_name": output_bus_name,
        "dispatchable": True,
        "timeseries": timeseries,
        # "opex_var": {"value": price, UNIT: "currency/unit"},
        "lifetime": {
            "value": dict_values["economic_data"][PROJECT_DURATION]["value"],
            UNIT: "year",
        },
    }

    # check if multiple busses are provided
    # for each bus, read time series for opex_var if a file name has been
    # provided in energy price
    if isinstance(price["value"], list):
        source.update({"opex_var": {"value": [], UNIT: price[UNIT]}})
        values_info = []
        for element in price["value"]:
            if isinstance(element, dict):
                source["opex_var"]["value"].append(
                    get_timeseries_multiple_flows(
                        dict_values[SIMULATION_SETTINGS],
                        source,
                        element["file_name"],
                        element["header"],
                    )
                )
                values_info.append(element)
            else:
                source["opex_var"]["value"].append(element)
        if len(values_info) > 0:
            source["opex_var"]["values_info"] = values_info

    elif isinstance(price["value"], dict):
        source.update(
            {
                "opex_var": {
                    "value": {
                        "file_name": price["value"]["file_name"],
                        "header": price["value"]["header"],
                    },
                    UNIT: price[UNIT],
                }
            }
        )
        receive_timeseries_from_csv(
            dict_values, dict_values[SIMULATION_SETTINGS], source, "opex_var"
        )
    else:
        source.update({"opex_var": {"value": price["value"], UNIT: price[UNIT]}})

    logging.debug(
        "Asset %s: sum of timeseries = %s", asset_name, sum(timeseries.values)
    )

    if "opex_fix" in kwargs or "capex_var" in kwargs:
        if "opex_fix" in kwargs:
            source.update({"opex_fix": kwargs["opex_fix"]})
        else:
            source.update({"capex_var": kwargs["capex_var"]})

        source.update(
            {
                "optimizeCap": {"value": True, UNIT: "bool"},
                "timeseries_peak": {"value": max(timeseries), UNIT: "kW"},
                # todo if we have normalized timeseries hiere, the capex/opex (simulation) have changed, too
                "timeseries_normalized": timeseries / max(timeseries),
            }
        )
        if type(source["opex_var"]["value"]) == pd.Series:
            logging.warning(
                "Attention! %s is created, with a price defined as a timeseries (average: %s). "
                "If this is DSO supply, this could be improved. Please refer to Issue #23.",
                source[LABEL],
                source["opex_var"]["value"].mean(),
            )
        else:
            logging.warning(
                "Attention! %s is created, with a price of %s."
                "If this is DSO supply, this could be improved. Please refer to Issue #23. ",
                source[LABEL],
                source["opex_var"]["value"],
            )
    else:
        source.update({"optimizeCap": {"value": False, UNIT: "bool"}})

    # add the parameter "maximumCap" to DSO source
    source.update({"maximumCap": {"value": None, UNIT: "kWp"}})

    # update dictionary
    dict_values[ENERGY_PRODUCTION].update({asset_name: source})

    # create new input bus if non-existent before. Check if multiple busses are provided
    if isinstance(output_bus, list):
        for bus in output_bus:
            # add to list of assets on busses
            update_bus(dict_values, bus, asset_name, source[LABEL])
    else:
        # add to list of assets on busses
        update_bus(dict_values, output_bus, asset_name, source[LABEL])

    return


def define_sink(dict_values, asset_name, price, input_bus, **kwargs):
    """

    :param dict_values:
    :param asset_name:
    :param price:
    :param input_bus:
    :param kwargs:
    :return:
    """
    # create name of bus. Check if multiple busses are given
    if isinstance(input_bus, list):
        input_bus_name = []
        for bus in input_bus:
            input_bus_name.append(bus_suffix(bus))
    else:
        input_bus_name = bus_suffix(input_bus)

    # create a dictionary for the sink
    sink = {
        OEMOF_ASSET_TYPE: OEMOF_SINK,
        LABEL: asset_name + "_sink",
        "input_direction": input_bus,
        "input_bus_name": input_bus_name,
        # "opex_var": {"value": price, UNIT: "currency/kWh"},
        "lifetime": {
            "value": dict_values["economic_data"][PROJECT_DURATION]["value"],
            UNIT: "year",
        },
    }

    # check if multiple busses are provided
    # for each bus, read time series for opex_var if a file name has been provided in feedin tariff
    if isinstance(price["value"], list):
        sink.update({"opex_var": {"value": [], UNIT: price[UNIT]}})
        values_info = []
        for element in price["value"]:
            if isinstance(element, dict):
                timeseries = get_timeseries_multiple_flows(
                    dict_values[SIMULATION_SETTINGS],
                    sink,
                    element["file_name"],
                    element["header"],
                )
                if asset_name[-6:] == "feedin":
                    sink["opex_var"]["value"].append([-i for i in timeseries])
                else:
                    sink["opex_var"]["value"].append(timeseries)
            else:
                sink["opex_var"]["value"].append(element)
        if len(values_info) > 0:
            sink["opex_var"]["values_info"] = values_info

    elif isinstance(price["value"], dict):
        sink.update(
            {
                "opex_var": {
                    "value": {
                        "file_name": price["value"]["file_name"],
                        "header": price["value"]["header"],
                    },
                    UNIT: price[UNIT],
                }
            }
        )
        receive_timeseries_from_csv(
            dict_values, dict_values[SIMULATION_SETTINGS], sink, "opex_var"
        )
        if (
            asset_name[-6:] == "feedin"
        ):  # change into negative value if this is a feedin sink
            sink["opex_var"].update({"value": [-i for i in sink["opex_var"]["value"]]})
    else:
        if asset_name[-6:] == "feedin":
            value = -price["value"]
        else:
            value = price["value"]
        sink.update({"opex_var": {"value": value, UNIT: price[UNIT]}})

    if "capex_var" in kwargs:
        sink.update(
            {
                "capex_var": kwargs["capex_var"],
                "optimizeCap": {"value": True, UNIT: "bool"},
            }
        )
    if "opex_fix" in kwargs:
        sink.update(
            {
                "opex_fix": kwargs["opex_fix"],
                "optimizeCap": {"value": True, UNIT: "bool"},
            }
        )
    else:
        sink.update({"optimizeCap": {"value": False, UNIT: "bool"}})

    # update dictionary
    dict_values[ENERGY_CONSUMPTION].update({asset_name: sink})

    # If multiple input busses exist
    if isinstance(input_bus, list):
        for bus in input_bus:
            update_bus(dict_values, bus, asset_name, sink[LABEL])
    else:
        # add to list of assets on busses
        update_bus(dict_values, input_bus, asset_name, sink[LABEL])

    return


def evaluate_lifetime_costs(settings, economic_data, dict_asset):
    """

    :param settings:
    :param economic_data:
    :param dict_asset:
    :return:
    """

    complete_missing_cost_data(dict_asset)

    determine_lifetime_opex_var(dict_asset, economic_data)

    dict_asset.update(
        {
            "lifetime_capex_var": {
                "value": economics.capex_from_investment(
                    dict_asset["capex_var"]["value"],
                    dict_asset["lifetime"]["value"],
                    economic_data[PROJECT_DURATION]["value"],
                    economic_data[DISCOUNTFACTOR]["value"],
                    economic_data[TAX]["value"],
                ),
                UNIT: dict_asset["capex_var"][UNIT],
            }
        }
    )

    # Annuities of components including opex AND capex #
    dict_asset.update(
        {
            "annuity_capex_opex_var": {
                "value": economics.annuity(
                    dict_asset["lifetime_capex_var"]["value"],
                    economic_data["crf"]["value"],
                )
                + dict_asset["opex_fix"]["value"],  # changes from opex_var
                UNIT: dict_asset["lifetime_capex_var"][UNIT] + "/a",
            }
        }
    )

    dict_asset.update(
        {
            "lifetime_opex_fix": {
                "value": dict_asset["opex_fix"]["value"]
                * economic_data["annuity_factor"]["value"],
                UNIT: dict_asset["opex_fix"][UNIT][:-2],
            }
        }
    )

    dict_asset.update(
        {
            "simulation_annuity": {
                "value": economics.simulation_annuity(
                    dict_asset["annuity_capex_opex_var"]["value"],
                    settings["evaluated_period"]["value"],
                ),
                UNIT: "currency/unit/simulation period",
            }
        }
    )

    return


def complete_missing_cost_data(dict_asset):
    # todo check if this can be deleted
    if "capex_var" not in dict_asset:
        dict_asset.update({"capex_var": 0})
        logging.error(
            "Dictionary of asset %s is incomplete, as capex_var is missing.",
            dict_asset[LABEL],
        )
    if "opex_fix" not in dict_asset:
        dict_asset.update({"opex_fix": 0})
        logging.error(
            "Dictionary of asset %s is incomplete, as opex_fix is missing.",
            dict_asset[LABEL],
        )
    return


def determine_lifetime_opex_var(dict_asset, economic_data):
    """
    #todo I am not sure that this makes sense. is this used in d0?
    Parameters
    ----------
    dict_asset
    economic_data

    Returns
    -------

    """
    if isinstance(dict_asset["opex_var"]["value"], float) or isinstance(
        dict_asset["opex_var"]["value"], int
    ):
        lifetime_opex_var = get_lifetime_opex_var_one_value(dict_asset, economic_data)

    elif isinstance(dict_asset["opex_var"]["value"], list):
        lifetime_opex_var = get_lifetime_opex_var_list(dict_asset, economic_data)

    elif isinstance(dict_asset["opex_var"]["value"], pd.Series):
        lifetime_opex_var = get_lifetime_opex_var_timeseries(dict_asset, economic_data)

    else:
        raise ValueError(
            f'Type of opex_var neither int, float, list or pd.Series, but of type {dict_asset["opex_var"]["value"]}. Is type correct?'
        )

    dict_asset.update({"lifetime_opex_var": {"value": lifetime_opex_var, UNIT: "?",}})
    return


def get_lifetime_opex_var_one_value(dict_asset, economic_data):
    """
    opex_var can be a fix value
    Returns
    -------

    """
    lifetime_opex_var = (
        dict_asset["opex_var"]["value"] * economic_data["annuity_factor"]["value"]
    )
    return lifetime_opex_var


def get_lifetime_opex_var_list(dict_asset, economic_data):
    """
    opex_var can be a list, for example if there are two input flows to a component, eg. water and electricity.
    Their ratio for providing cooling in kWh therm is fix. There should be a lifetime_opex_var for each of them.

    Returns
    -------

    """

    # if multiple busses are provided, it takes the first opex_var (corresponding to the first bus)

    first_value = dict_asset["opex_var"]["value"][0]
    if isinstance(first_value, float) or isinstance(first_value, int):
        opex_var = first_value
    else:
        opex_var = sum(first_value) / len(first_value)

    lifetime_opex_var = opex_var * economic_data["annuity_factor"]["value"]
    return lifetime_opex_var


def get_lifetime_opex_var_timeseries(dict_asset, economic_data):
    """
    opex_var can be a timeseries, eg. in case that there is an hourly pricing
    Returns
    -------

    """
    # take average value of opex_var if it is a timeseries

    opex_var = sum(dict_asset["opex_var"]["value"]) / len(
        dict_asset["opex_var"]["value"]
    )
    lifetime_opex_var = (
        dict_asset["opex_var"]["value"] * economic_data["annuity_factor"]["value"]
    )
    return lifetime_opex_var


# read timeseries. 2 cases are considered: Input type is related to demand or generation profiles,
# so additional values like peak, total or average must be calculated. Any other type does not need this additional info.
def receive_timeseries_from_csv(
    dict_values, settings, dict_asset, type, is_demand_profile=False
):
    """

    :param settings:
    :param dict_asset:
    :param type:
    :return:
    """
    if type == "input" and "input" in dict_asset:
        file_name = dict_asset[type]["file_name"]
        header = dict_asset[type]["header"]
        unit = dict_asset[type][UNIT]
    elif "file_name" in dict_asset:
        # todo this input/file_name thing is a workaround and has to be improved in the future
        # if only filename is given here, then only one column can be in the csv
        file_name = dict_asset["file_name"]
        unit = dict_asset[UNIT] + "/h"
    else:
        file_name = dict_asset[type]["value"]["file_name"]
        header = dict_asset[type]["value"]["header"]
        unit = dict_asset[type][UNIT]

    file_path = os.path.join(settings["path_input_folder"], TIME_SERIES, file_name)
    verify.lookup_file(file_path, dict_asset[LABEL])

    data_set = pd.read_csv(file_path, sep=",")

    if "file_name" in dict_asset:
        header = data_set.columns[0]

    if len(data_set.index) == settings["periods"]:
        if type == "input":
            dict_asset.update(
                {
                    "timeseries": pd.Series(
                        data_set[header].values, index=settings["time_index"]
                    )
                }
            )
        else:
            dict_asset[type]["value_info"] = dict_asset[type]["value"]
            dict_asset[type]["value"] = pd.Series(
                data_set[header].values, index=settings["time_index"]
            )

        logging.debug("Added timeseries of %s (%s).", dict_asset[LABEL], file_path)
    elif len(data_set.index) >= settings["periods"]:
        if type == "input":
            dict_asset.update(
                {
                    "timeseries": pd.Series(
                        data_set[header][0 : len(settings["time_index"])].values,
                        index=settings["time_index"],
                    )
                }
            )
        else:
            dict_asset[type]["value_info"] = dict_asset[type]["value"]
            dict_asset[type]["value"] = pd.Series(
                data_set[header][0 : len(settings["time_index"])].values,
                index=settings["time_index"],
            )

        logging.info(
            "Provided timeseries of %s (%s) longer than evaluated period. "
            "Excess data dropped.",
            dict_asset[LABEL],
            file_path,
        )

    elif len(data_set.index) <= settings["periods"]:
        logging.critical(
            "Input error! "
            "Provided timeseries of %s (%s) shorter then evaluated period. "
            "Operation terminated",
            dict_asset[LABEL],
            file_path,
        )
        sys.exit()

    if type == "input":
        dict_asset.update(
            {
                "timeseries_peak": {
                    "value": max(dict_asset["timeseries"]),
                    UNIT: unit,
                },
                "timeseries_total": {
                    "value": sum(dict_asset["timeseries"]),
                    UNIT: unit,
                },
                "timeseries_average": {
                    "value": sum(dict_asset["timeseries"])
                    / len(dict_asset["timeseries"]),
                    UNIT: unit,
                },
            }
        )

        if dict_asset["optimizeCap"]["value"] == True:
            logging.debug("Normalizing timeseries of %s.", dict_asset[LABEL])
            dict_asset.update(
                {
                    "timeseries_normalized": dict_asset["timeseries"]
                    / dict_asset["timeseries_peak"]["value"]
                }
            )
            # just to be sure!
            if any(dict_asset["timeseries_normalized"].values) > 1:
                logging.warning(
                    "Error, %s timeseries not normalized, greater than 1.",
                    dict_asset[LABEL],
                )
            if any(dict_asset["timeseries_normalized"].values) < 0:
                logging.warning("Error, %s timeseries negative.", dict_asset[LABEL])

    # plot all timeseries that are red into simulation input
    try:
        plot_input_timeseries(
            dict_values,
            settings,
            dict_asset["timeseries"],
            dict_asset[LABEL],
            header,
            is_demand_profile,
        )
    except:
        plot_input_timeseries(
            dict_values,
            settings,
            dict_asset[type]["value"],
            dict_asset[LABEL],
            header,
            is_demand_profile,
        )

    # copy input files
    shutil.copy(
        file_path, os.path.join(settings["path_output_folder"], INPUTS_COPY, file_name)
    )
    logging.debug("Copied timeseries %s to output folder / inputs.", file_path)
    return


def plot_input_timeseries(
    dict_values, user_input, timeseries, asset_name, column_head, is_demand_profile
):
    logging.info("Creating plots for asset %s's parameter %s", asset_name, column_head)
    fig, axes = plt.subplots(nrows=1, figsize=(16 / 2.54, 10 / 2.54 / 2))
    axes_mg = axes

    timeseries.plot(
        title=asset_name, ax=axes_mg, drawstyle="steps-mid",
    )
    axes_mg.set(xlabel="Time", ylabel=column_head)
    path = os.path.join(
        user_input["path_output_folder"],
        "input_timeseries_" + asset_name + "_" + column_head + ".png",
    )
    if is_demand_profile is True:
        dict_values[PATHS_TO_PLOTS][PLOTS_DEMANDS] += [str(path)]
    else:
        dict_values[PATHS_TO_PLOTS][PLOTS_RESOURCES] += [str(path)]
    plt.savefig(
        path, bbox_inches="tight",
    )

    plt.close()
    plt.clf()
    plt.cla()


def treat_multiple_flows(dict_asset, dict_values, parameter):
    """
    This function consider the case a technical parameter on the json file has a list of values because multiple
    inputs or outputs busses are considered.
    Parameters
    ----------
    dict_values:
    dictionary of current values of the asset
    parameter:
    usually efficiency. Different efficiencies will be given if an asset has multiple inputs or outputs busses,
    so a list must be considered.

    Returns
    -------

    """
    updated_values = []
    values_info = (
        []
    )  # filenames and headers will be stored to allow keeping track of the timeseries generation
    for element in dict_asset[parameter]["value"]:
        if isinstance(element, dict):
            updated_values.append(
                get_timeseries_multiple_flows(
                    dict_values[SIMULATION_SETTINGS],
                    dict_asset,
                    element["file_name"],
                    element["header"],
                )
            )
            values_info.append(element)
        else:
            updated_values.append(element)
    dict_asset[parameter]["value"] = updated_values
    if len(values_info) > 0:
        dict_asset[parameter].update({"values_info": values_info})

    return


# reads timeseries specifically when the need comes from a multiple or output busses situation
# returns the timeseries. Does not update any dictionary
def get_timeseries_multiple_flows(settings, dict_asset, file_name, header):
    """

    Parameters
    ----------
    dict_asset:
    dictionary of the asset
    file_name:
    name of the file to read the time series
    header:
    name of the column where the timeseries is provided

    Returns
    -------

    """
    file_path = os.path.join(settings["path_input_folder"], TIME_SERIES, file_name)
    verify.lookup_file(file_path, dict_asset[LABEL])

    data_set = pd.read_csv(file_path, sep=",")
    if len(data_set.index) == settings["periods"]:
        return pd.Series(data_set[header].values, index=settings["time_index"])
    elif len(data_set.index) >= settings["periods"]:
        return pd.Series(
            data_set[header][0 : len(settings["time_index"])].values,
            index=settings["time_index"],
        )
    elif len(data_set.index) <= settings["periods"]:
        logging.critical(
            "Input error! "
            "Provided timeseries of %s (%s) shorter then evaluated period. "
            "Operation terminated",
            dict_asset[LABEL],
            file_path,
        )
        sys.exit()


def add_maximum_cap(dict_values, group, asset, subasset=None):
    """
    Checks if maximumCap is in the csv file and if not, adds it to the dict

    Parameters
    ----------
    dict_values: dict
        dictionary of all assets
    asset: str
        asset name
    subasset: str
        subasset name

    Returns
    -------

    """
    if subasset is None:
        dict = dict_values[group][asset]
    else:
        dict = dict_values[group][asset][subasset]
    if "maximumCap" in dict:
        # check if maximumCap is greater that installedCap
        if dict["maximumCap"]["value"] is not None:
            if dict["maximumCap"]["value"] < dict["installedCap"]["value"]:

                logging.warning(
                    f"The stated maximumCap in {group} {asset} is smaller than the "
                    "installedCap. Please enter a greater maximumCap."
                    "For this simulation, the maximumCap will be "
                    "disregarded and not be used in the simulation"
                )
                dict["maximumCap"]["value"] = None
            # check if maximumCao is 0
            elif dict["maximumCap"]["value"] == 0:
                logging.warning(
                    f"The stated maximumCap of zero in {group} {asset} is invalid."
                    "For this simulation, the maximumCap will be "
                    "disregarded and not be used in the simulation."
                )
                dict["maximumCap"]["value"] = None
    else:
        dict.update({"maximumCap": {"value": None, UNIT: dict[UNIT]}})
