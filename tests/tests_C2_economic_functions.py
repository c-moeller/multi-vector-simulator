import src.C2_economic_functions as e_functions

project_life = 20
wacc = 0.1
investment_t0 = 220000
tax = 0.15
# please do not change project_life and lifetime as this will affect CAPEX calculations that depend on the number of investments
lifetime = {
    "equal project life": project_life,
    "smaller project life": project_life - 5,
    "bigger project life": project_life + 15,
}
present_value = 295000
crf = 0.12
annuity = 35400
annuity_factor = 1 / 0.12
fuel_keys = {
    "fuel_price": 1.3,
    "fuel_price_change_annual": 0,
    "project_lifetime": project_life,
    "wacc": wacc,
}


def test_annuity_factor():
    """

    Tests whether the MVS is correctly calculating the annuity factor
    """
    AF = e_functions.annuity_factor(project_life, wacc)
    assert AF == 1 / wacc - 1 / (wacc * (1 + wacc) ** project_life)


def test_crf():
    """

    Tests whether the MVS is correctly calculating the capital recovery factor
    """
    CRF = e_functions.crf(project_life, wacc)
    assert CRF == (wacc * (1 + wacc) ** project_life) / ((1 + wacc) ** project_life - 1)


def test_capex_from_investment_lifetime_equals_project_life():
    """

    Tests whether the MVS is correctly calculating the capital expenditure of the project if the lifetime is equal to project_life
    """
    CAPEX = e_functions.capex_from_investment(
        investment_t0, lifetime["equal project life"], project_life, wacc, tax
    )
    assert CAPEX == investment_t0 * (1 + tax)


def test_capex_from_investment_lifetime_smaller_than_project_life():
    """

    Tests whether the MVS is correctly calculating the capital expenditure of the project if the lifetime is smaller than project_life
    """
    CAPEX = e_functions.capex_from_investment(
        investment_t0, lifetime["smaller project life"], project_life, wacc, tax
    )
    first_investment = investment_t0 * (1 + tax)
    assert CAPEX == first_investment + first_investment / (1 + wacc) ** (
        1 * lifetime["smaller project life"]
    ) + first_investment + first_investment / (1 + wacc) ** (
        1 * lifetime["smaller project life"]
    ) + first_investment / (
        1 + wacc
    ) ** (
        2 * lifetime["smaller project life"]
    ) - (
        (
            first_investment
            / ((1 + wacc) ** ((2 - 1) * lifetime["smaller project life"]))
        )
        / lifetime["smaller project life"]
    ) * (
        2 * lifetime["smaller project life"] - project_life
    )


def test_capex_from_investment_lifetime_bigger_than_project_life():
    """

    Tests whether the MVS is correctly calculating the capital expenditure of the project if the lifetime is bigger than project_life
    """
    CAPEX = e_functions.capex_from_investment(
        investment_t0, lifetime["bigger project life"], project_life, wacc, tax
    )
    first_investment = investment_t0 * (1 + tax)
    assert CAPEX == first_investment + first_investment / (1 + wacc) ** (
        1 * lifetime["bigger project life"]
    ) - (
        (first_investment / ((1 + wacc) ** ((1 - 1) * lifetime["bigger project life"])))
        / lifetime["bigger project life"]
    ) * (
        1 * lifetime["bigger project life"] - project_life
    )


def test_annuity():
    """

    Tests whether the MVS is correctly calculating the annuity value
    """
    A = e_functions.annuity(present_value, crf)
    assert A == present_value * crf


def test_present_value_from_annuity():
    """

    Tests whether the MVS is correctly calculating the present value
    """
    PV_from_annuity = e_functions.present_value_from_annuity(annuity, annuity_factor)
    assert PV_from_annuity == annuity * annuity_factor


def test_fuel_price_present_value_without_fuel_price_change(economics):
    """

    Tests whether the MVS is correctly calculating the present value of the fuel price over the lifetime of the project without fuel price change
    """
    PV_fuel_price = e_functions.fuel_price_present_value(fuel_keys)
    assert PV_fuel_price == fuel_keys["fuel_price"]

    # fuel_keys["fuel_price_change_annual"] = 0.008
    # fuel_keys["crf"] = e_functions.crf(project_life, wacc)
    # PV_fuel_price = e_functions.fuel_price_present_value(fuel_keys)
    # still not sure how not to use float number in assert
    # assert PV_fuel_price == 13.06049955 * ((0.1 * (1 + 0.1) ** 20) / ((1 + 0.1) ** 20 - 1))
