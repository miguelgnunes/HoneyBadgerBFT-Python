## HoneybadgerBFT protocol as consensus algorithm for [BFT Fabric Ordering Service](https://github.com/miguelgnunes/fabric-orderingservice)

This is a fork of the [HoneybadgerBFT protocol](https://github.com/initc3/HoneyBadgerBFT-Python) with a simple adaptation to work as the
consensus layer of **BFT Fabric Ordering Service**. When starting up, this software creates a connection to a running *BFT Fabric Ordering Service Frontend*
and awaits for transactions for ordering. As soon as each transaction is ordered, it returns it back to the Frontend. 

### How to run it

We have created our setup with Docker for easy deployment. After starting up the [*BFT Fabric Ordering Service Frontend*](https://github.com/miguelgnunes/fabric-orderingservice)
follow the below instructions:

1. From the project base folder, Run:
```bash
$ docker build -t honeybadgerbft-python-base -f ./miguel_Dockerfile_base .
$ docker build -t honeybadgerbft-python -f ./miguel_Dockerfile
```

The first command will build a base image with HoneybadgerBFT base code as well as set the default algorithm parameters as environment variables.
The second command will build the image we will actually use to run the algorithm, which uses the first built image as base.

2. Run:
```bash
$ docker run -e N="4" -e f="1" -e B="16" -e i="bft.frontend.1000" -e p="5001" -it --network="bftchannel" honeybadgerbft-python
```

This will run a local cluster of Honeybadger instances. ***N*** is the number of nodes, ***f*** is the max number of byzantine nodes,
***B*** is the batch size, ***i*** is the hostname of the [BFT Fabric Ordering Service Frontend](https://github.com/miguelgnunes/fabric-orderingservice)
and ***p*** is the port where host ***i*** is listening for connections. The parameters set for Frontend host and port are 
suited to the example parameters as set in the example tutorial for [*BFT Fabric Ordering Service Frontend*](https://github.com/miguelgnunes/fabric-orderingservice).
