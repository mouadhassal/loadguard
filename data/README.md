# Data

`raw_sintef/sessions.csv` is "Dataset 1_EV charging reports.csv" from:

Å. L. Sørensen, K. B. Lindberg, I. Sartori, I. Andresen. *Residential electric vehicle charging datasets from apartment buildings.* SINTEF, Mendeley Data, V2 (2021). doi:10.17632/jbks2rcwyj.2. Licensed CC BY 4.0. The companion article is *Data in Brief* 36 (2021) 107105.

**Contents:** 6,878 charging sessions (Dec 2018 to Jan 2020) from apartment-building garages in Trondheim, Norway. Each session has a plug-in time, a plug-out time and its energy in kWh.

**Changes made here:** none to the file itself. The study keeps only `User_type == Private` sessions and caps each session's energy at 7.2 kW × its stay (see `src/loadguard/study.py`).
