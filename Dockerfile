FROM nrel/energyplus:8.9.0

RUN apt update

RUN apt install software-properties-common -y

RUN add-apt-repository ppa:deadsnakes/ppa

RUN apt update

RUN apt install python3.7 -y

RUN apt install python3-pip -y

RUN pip3 install --upgrade pip

RUN mkdir /var/simdata/data

RUN mkdir /app

COPY ./requirements.txt /app

WORKDIR /app

RUN pip3 install -r requirements.txt

# COPY ./eplus.idd /var/simdata/data/eplus.idd

RUN apt-get install -y tzdata

COPY ./app.py /app/app.py
